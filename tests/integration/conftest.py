"""Fixtures for the integration suite: Docker, a local registry, NPM, a GitHub stub and an in-process UAM.

Image names are overridable (the defaults are the plain Docker Hub names used in CI):
`UAM_TEST_REGISTRY_IMAGE`, `UAM_TEST_NPM_IMAGE`, `UAM_TEST_BASE_IMAGE`.
`UAM_TEST_NPM_TIMEOUT` (seconds, default 240) bounds the wait for NPM; NPM-dependent assertions are
skipped when it does not come up (set `UAM_TEST_REQUIRE_NPM=1`, as CI does, to fail instead).
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import docker
import docker.errors
import httpx
import pytest
from fastapi.testclient import TestClient

from tests.integration.stub_github import StubGitHub
from tests.integration.support import (
    BASE_DOMAIN,
    NPM_LOGIN,
    PORT_RANGE,
    TEST_PREFIX,
    LocalRegistry,
    NpmApi,
    Uam,
    free_port,
    wait_for,
)
from uam.app import AppConfig, create_app

REGISTRY_IMAGE = os.environ.get("UAM_TEST_REGISTRY_IMAGE", "registry:2")
NPM_IMAGE = os.environ.get("UAM_TEST_NPM_IMAGE", "jc21/nginx-proxy-manager:2.12.3")
BASE_IMAGE = os.environ.get("UAM_TEST_BASE_IMAGE", "alpine:3.20")
NPM_TIMEOUT = float(os.environ.get("UAM_TEST_NPM_TIMEOUT", "240"))
_INFRA = {f"{TEST_PREFIX}registry", f"{TEST_PREFIX}npm"}


@pytest.fixture(scope="session", autouse=True)
def _local_traffic_bypasses_proxy() -> Iterator[None]:
    """UAM's own HTTP clients must reach localhost and the docker bridge directly."""
    patch = pytest.MonkeyPatch()
    extra = "localhost,127.0.0.1,172.17.0.1"
    for key in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(key, "")
        patch.setenv(key, f"{extra},{current}" if current else extra)
    yield
    patch.undo()


@pytest.fixture(scope="session")
def docker_client() -> Iterator[docker.DockerClient]:
    """The Docker SDK client; the whole suite is skipped when no daemon answers."""
    try:
        client = docker.from_env()
        client.ping()
    except docker.errors.DockerException as exc:
        pytest.skip(f"no Docker daemon available: {exc}")
    yield client
    client.close()


def _remove_container(client: docker.DockerClient, name: str) -> None:
    try:
        client.containers.get(name).remove(force=True, v=True)
    except docker.errors.NotFound:
        pass


@pytest.fixture(scope="session", autouse=True)
def _image_cleanup(docker_client: docker.DockerClient) -> Iterator[None]:
    """Remove every image created during the session (built, pushed or pulled) at the end."""
    before = {i.id for i in docker_client.images.list(all=True)}
    yield
    for image in docker_client.images.list(all=True):
        if image.id in before:
            continue
        try:
            docker_client.images.remove(str(image.id), force=True)
        except docker.errors.DockerException:
            pass


@pytest.fixture(autouse=True)
def _container_cleanup(docker_client: docker.DockerClient) -> Iterator[None]:
    """After each test, remove every `uamtest-*` container except the shared registry and NPM."""
    yield
    for c in docker_client.containers.list(all=True):
        if (c.name or "").startswith(TEST_PREFIX) and c.name not in _INFRA:
            try:
                c.remove(force=True, v=True)
            except docker.errors.DockerException:
                pass


@pytest.fixture(scope="session")
def registry(docker_client: docker.DockerClient) -> Iterator[LocalRegistry]:
    """A local `registry:2` standing in for GHCR."""
    port = free_port()
    name = f"{TEST_PREFIX}registry"
    _remove_container(docker_client, name)
    docker_client.containers.run(
        REGISTRY_IMAGE, name=name, detach=True, ports={"5000/tcp": ("127.0.0.1", port)}
    )
    reg = LocalRegistry(port, BASE_IMAGE)
    try:
        wait_for(
            lambda: httpx.get(f"http://{reg.host}/v2/", trust_env=False, timeout=3).status_code == 200,
            timeout=60,
            what="the local registry",
        )
        yield reg
    finally:
        _remove_container(docker_client, name)


@pytest.fixture(scope="session")
def gateway_ip(docker_client: docker.DockerClient) -> str:
    """The docker0 gateway: how containers (NPM) reach ports published on the host."""
    bridge = docker_client.networks.get("bridge")
    return str(bridge.attrs["IPAM"]["Config"][0]["Gateway"])


@pytest.fixture(scope="session")
def npm_env(docker_client: docker.DockerClient) -> Iterator[NpmApi | None]:
    """A real Nginx Proxy Manager container, or None when it does not come up within the timeout."""
    admin, proxy = free_port(), free_port()
    name = f"{TEST_PREFIX}npm"
    _remove_container(docker_client, name)
    docker_client.containers.run(
        NPM_IMAGE,
        name=name,
        detach=True,
        environment={"DISABLE_IPV6": "true"},
        volumes={
            f"{name}-data": {"bind": "/data", "mode": "rw"},
            f"{name}-le": {"bind": "/etc/letsencrypt", "mode": "rw"},
        },
        ports={"81/tcp": ("127.0.0.1", admin), "80/tcp": ("127.0.0.1", proxy)},
    )
    api = NpmApi(admin, proxy)
    try:
        try:
            wait_for(
                api.login,
                timeout=NPM_TIMEOUT,
                interval=3,
                what="Nginx Proxy Manager to accept the default login",
            )
        except TimeoutError as exc:
            logs = docker_client.containers.get(name).logs(tail=15).decode(errors="replace")
            if os.environ.get("UAM_TEST_REQUIRE_NPM") == "1":
                pytest.fail(f"NPM did not come up: {exc}\n{logs}")
            warnings.warn(f"NPM did not come up, NPM assertions are skipped: {exc}\n{logs}", stacklevel=1)
            yield None
            return
        yield api
    finally:
        _remove_container(docker_client, name)
        for vol in (f"{name}-data", f"{name}-le"):
            try:
                docker_client.volumes.get(vol).remove(force=True)
            except docker.errors.DockerException:
                pass


@pytest.fixture
def npm(npm_env: NpmApi | None) -> Iterator[NpmApi | None]:
    """The NPM handle for one test; any proxy host left behind is deleted afterwards."""
    yield npm_env
    if npm_env is not None:
        try:
            npm_env.login()
            for host in npm_env.hosts():
                npm_env.delete_host(int(host["id"]))
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the test result
            warnings.warn(f"could not clean NPM hosts: {exc}", stacklevel=1)


@pytest.fixture(scope="session")
def github() -> Iterator[StubGitHub]:
    """The GitHub stub, serving on a free local port."""
    stub = StubGitHub()
    stub.start()
    yield stub
    stub.stop()


@pytest.fixture
def uam(
    tmp_path: Path,
    github: StubGitHub,
    registry: LocalRegistry,  # noqa: ARG001 - settings below assume the registry exists
    gateway_ip: str,
    npm: NpmApi | None,
) -> Iterator[Uam]:
    """An in-process UAM wired to the real Docker daemon, NPM and the GitHub stub, already set up."""
    config = AppConfig(
        data_dir=tmp_path / "data",
        github_api_url=github.url,
        start_scheduler=False,
        proc_root="/proc",
    )
    with TestClient(create_app(config), headers={"X-Requested-With": "uam"}) as client:
        driver = Uam(client, tmp_path)
        driver.flash.mkdir()
        driver.api("POST", "/setup", json={"password": "correct horse battery"})
        settings: dict[str, Any] = driver.api("GET", "/settings")
        settings.update(
            port_range_start=PORT_RANGE[0],
            port_range_end=PORT_RANGE[1],
            host_ip=gateway_ip,
            appdata_root=str(driver.appdata),
            template_export_dir=str(driver.flash),
            base_domain=BASE_DOMAIN,
        )
        if npm is not None:
            npm.login()
            cred = driver.api(
                "POST",
                "/credentials",
                json={"name": "npm", "kind": "npm", "username": NPM_LOGIN[0], "secret": NPM_LOGIN[1]},
            )
            settings.update(npm_url=npm.admin_url, npm_credential_id=cred["id"])
        driver.api("PUT", "/settings", json=settings)
        yield driver
