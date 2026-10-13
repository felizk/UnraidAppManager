"""Unit tests for SdkDockerEngine (SDK client mocked) plus a real-Docker smoke test."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import docker.errors
import pytest

from uam.clients.docker_engine import SdkDockerEngine, build_create_kwargs
from uam.domain import ContainerSpec, Mount, PortBinding, RegistryAuth
from uam.errors import AuthError, ConflictError, NotFound


def api_error(status: int, msg: str = "boom") -> docker.errors.APIError:
    resp = MagicMock()
    resp.status_code = status
    return docker.errors.APIError(msg, response=resp, explanation=msg)


class Clock:
    """Fake monotonic clock; `sleep` advances it."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def make_engine(client: Any | None = None, clock: Clock | None = None, **kw: Any) -> SdkDockerEngine:
    clock = clock or Clock()
    return SdkDockerEngine(client or MagicMock(), sleep=clock.sleep, monotonic=clock.monotonic, **kw)


# ---------- pull ----------
async def test_pull_passes_auth_and_returns_matching_digest() -> None:
    client = MagicMock()
    client.images.pull.return_value.attrs = {
        "RepoDigests": ["other/app@sha256:" + "a" * 64, "ghcr.io/me/app@sha256:" + "b" * 64]
    }
    engine = make_engine(client)
    digest = await engine.pull(
        "ghcr.io/me/app:latest", RegistryAuth(username="u", password="p", registry="ghcr.io")
    )
    assert digest == "sha256:" + "b" * 64
    client.images.pull.assert_called_once_with(
        "ghcr.io/me/app:latest", auth_config={"username": "u", "password": "p"}
    )


async def test_pull_without_auth_and_docker_hub_normalisation() -> None:
    client = MagicMock()
    client.images.pull.return_value.attrs = {"RepoDigests": ["nginx@sha256:" + "c" * 64]}
    digest = await make_engine(client).pull("docker.io/library/nginx:1.27", None)
    assert digest == "sha256:" + "c" * 64
    client.images.pull.assert_called_once_with("docker.io/library/nginx:1.27")


async def test_pull_errors_are_translated() -> None:
    client = MagicMock()
    client.images.pull.side_effect = api_error(401)
    with pytest.raises(AuthError):
        await make_engine(client).pull("x/y:1", None)
    client.images.pull.side_effect = docker.errors.ImageNotFound("nope")
    with pytest.raises(NotFound):
        await make_engine(client).pull("x/y:1", None)


# ---------- create ----------
FULL_SPEC = ContainerSpec(
    name="app",
    image="ghcr.io/me/app:1",
    env={"A": "1"},
    ports=(
        PortBinding(8080, "tcp", 30001),
        PortBinding(53, "udp", 30002, host_ip="10.0.0.2"),
    ),
    mounts=(Mount("/mnt/user/appdata/app", "/config", "rw"), Mount("/mnt/media", "/media", "ro")),
    devices=("/dev/dri:/dev/dri:rwm",),
    labels={"uam.managed": "true"},
    network="br0",
    privileged=True,
    restart_policy="unless-stopped",
    hostname="myhost",
    cap_add=("NET_ADMIN",),
    memory_limit="512m",
    cpus=1.5,
    cpuset="0,1",
    log_opts={"max-size": "50m"},
    command=("--flag", "x"),
)


def test_create_kwargs_full_spec() -> None:
    assert build_create_kwargs(FULL_SPEC) == {
        "image": "ghcr.io/me/app:1",
        "name": "app",
        "detach": True,
        "environment": {"A": "1"},
        "labels": {"uam.managed": "true"},
        "network_mode": "br0",
        "privileged": True,
        "ports": {"8080/tcp": ("", 30001), "53/udp": ("10.0.0.2", 30002)},
        "volumes": [
            "/mnt/user/appdata/app:/config:rw",
            "/mnt/media:/media:ro",
        ],
        "devices": ["/dev/dri:/dev/dri:rwm"],
        "restart_policy": {"Name": "unless-stopped"},
        "hostname": "myhost",
        "cap_add": ["NET_ADMIN"],
        "mem_limit": "512m",
        "nano_cpus": 1_500_000_000,
        "cpuset_cpus": "0,1",
        "log_config": {"Type": "json-file", "Config": {"max-size": "50m"}},
        "command": ["--flag", "x"],
    }


def test_create_kwargs_minimal_and_host_network_ignores_ports() -> None:
    spec = ContainerSpec(
        name="a",
        image="i:1",
        network="host",
        ports=(PortBinding(80, "tcp", 30000),),
        restart_policy="on-failure:3",
    )
    kwargs = build_create_kwargs(spec)
    assert "ports" not in kwargs
    assert kwargs["network_mode"] == "host"
    assert kwargs["restart_policy"] == {"Name": "on-failure", "MaximumRetryCount": 3}
    for absent in (
        "volumes",
        "devices",
        "hostname",
        "cap_add",
        "mem_limit",
        "nano_cpus",
        "cpuset_cpus",
        "log_config",
        "command",
    ):
        assert absent not in kwargs


async def test_create_calls_sdk_and_connects_extra_networks() -> None:
    client = MagicMock()
    client.containers.create.return_value.id = "cid1"
    spec = ContainerSpec(name="app", image="i:1", extra_networks=("n1", "n2"))
    assert await make_engine(client).create(spec) == "cid1"
    client.containers.create.assert_called_once_with(**build_create_kwargs(spec))
    assert [c.args[0] for c in client.networks.get.call_args_list] == ["n1", "n2"]
    client.networks.get.return_value.connect.assert_called_with(client.containers.create.return_value)


async def test_create_conflict_and_cleanup_on_network_failure() -> None:
    client = MagicMock()
    client.containers.create.side_effect = api_error(409, "name in use")
    with pytest.raises(ConflictError):
        await make_engine(client).create(ContainerSpec(name="app", image="i:1"))

    client = MagicMock()
    client.networks.get.side_effect = docker.errors.NotFound("no net")
    with pytest.raises(NotFound):
        await make_engine(client).create(ContainerSpec(name="app", image="i:1", extra_networks=("x",)))
    client.containers.create.return_value.remove.assert_called_once_with(force=True)


# ---------- lifecycle ----------
async def test_lifecycle_calls() -> None:
    client = MagicMock()
    engine = make_engine(client)
    ctr = client.containers.get.return_value
    await engine.start("c")
    await engine.stop("c", timeout=5)
    await engine.remove("c", force=True)
    await engine.rename("c", "new")
    ctr.start.assert_called_once_with()
    ctr.stop.assert_called_once_with(timeout=5)
    ctr.remove.assert_called_once_with(force=True)
    ctr.rename.assert_called_once_with("new")
    ctr.rename.side_effect = api_error(409)
    with pytest.raises(ConflictError):
        await engine.rename("c", "new")
    client.containers.get.side_effect = docker.errors.NotFound("gone")
    with pytest.raises(NotFound):
        await engine.start("c")


async def test_logs_decodes() -> None:
    client = MagicMock()
    client.containers.get.return_value.logs.return_value = b"hi \xff\n"
    assert (await make_engine(client).logs("c", tail=5)).startswith("hi ")
    client.containers.get.return_value.logs.assert_called_once_with(tail=5, stdout=True, stderr=True)


# ---------- inspect ----------
def container_attrs(**over: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "Id": "abc123",
        "Name": "/web",
        "Image": "sha256:imgid",
        "Config": {
            "Image": "ghcr.io/me/web:latest",
            "Env": ["PATH=/usr/bin", "FOO=bar=baz", "TZ=UTC"],
            "Labels": {"net.unraid.docker.managed": "dockerman"},
        },
        "State": {"Status": "running", "Health": {"Status": "healthy"}},
        "HostConfig": {
            "NetworkMode": "br0",
            "RestartPolicy": {"Name": "on-failure", "MaximumRetryCount": 2},
            "Devices": [
                {"PathOnHost": "/dev/dri", "PathInContainer": "/dev/dri", "CgroupPermissions": "rwm"}
            ],
        },
        "Mounts": [
            {"Type": "bind", "Source": "/mnt/a", "Destination": "/a", "RW": True, "Propagation": "rprivate"},
            {"Type": "bind", "Source": "/mnt/b", "Destination": "/b", "RW": False, "Propagation": "rslave"},
            {"Type": "volume", "Name": "v", "Source": "/var/lib/docker/v", "Destination": "/v", "RW": True},
        ],
        "NetworkSettings": {
            "Ports": {
                "8080/tcp": [
                    {"HostIp": "0.0.0.0", "HostPort": "30001"},  # noqa: S104
                    {"HostIp": "::", "HostPort": "30001"},
                ],
                "9/udp": None,
            }
        },
    }
    attrs.update(over)
    return attrs


def image_attrs() -> dict[str, Any]:
    return {
        "RepoDigests": ["ghcr.io/me/web@sha256:" + "d" * 64],
        "Config": {
            "Env": ["PATH=/usr/bin"],
            "Labels": {"org.opencontainers.image.source": "https://github.com/me/web"},
        },
    }


async def test_inspect_fills_import_fields() -> None:
    client = MagicMock()
    client.containers.get.return_value.attrs = container_attrs()
    client.images.get.return_value.attrs = image_attrs()
    info = await make_engine(client).inspect("web")
    assert info is not None
    assert (info.id, info.name, info.image, info.state, info.health) == (
        "abc123",
        "web",
        "ghcr.io/me/web:latest",
        "running",
        "healthy",
    )
    assert info.image_digest == "sha256:" + "d" * 64
    assert info.image_id == "sha256:imgid"
    assert info.image_labels == {"org.opencontainers.image.source": "https://github.com/me/web"}
    assert info.env == {"FOO": "bar=baz", "TZ": "UTC"}
    assert info.mounts == (Mount("/mnt/a", "/a", "rw"), Mount("/mnt/b", "/b", "ro,slave"))
    assert info.devices == ("/dev/dri:/dev/dri:rwm",)
    assert info.network == "br0"
    assert info.restart_policy == "on-failure:2"
    assert info.published_ports == (PortBinding(8080, "tcp", 30001, host_ip="0.0.0.0"),)  # noqa: S104
    assert info.labels == {"net.unraid.docker.managed": "dockerman"}


async def test_inspect_missing_returns_none_and_no_healthcheck() -> None:
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("x")
    assert await make_engine(client).inspect("nope") is None

    client = MagicMock()
    client.containers.get.return_value.attrs = container_attrs(State={"Status": "exited"})
    client.images.get.side_effect = docker.errors.ImageNotFound("gone")
    info = await make_engine(client).inspect("web")
    assert info is not None and info.health is None and info.image_digest is None


async def test_list_containers_basic_fields() -> None:
    client = MagicMock()
    c1, c2 = MagicMock(), MagicMock()
    c1.attrs = container_attrs()
    c2.attrs = container_attrs(Id="def456", Name="/other")
    client.containers.list.return_value = [c1, c2]
    client.images.get.return_value.attrs = image_attrs()
    infos = await make_engine(client).list_containers(all=False)
    client.containers.list.assert_called_once_with(all=False)
    assert [i.name for i in infos] == ["web", "other"]
    assert infos[0].env == {} and infos[0].mounts == ()
    client.images.get.assert_called_once()  # cached per image id


# ---------- wait_healthy ----------
def state_client(states: list[dict[str, Any]], healthcheck: bool = False) -> MagicMock:
    """Each containers.get() yields the next attrs; the last repeats."""
    client = MagicMock()
    seq = list(states)

    def get(_id: str) -> MagicMock:
        ctr = MagicMock()
        ctr.attrs = seq.pop(0) if len(seq) > 1 else seq[0]
        return ctr

    client.containers.get.side_effect = get
    return client


def attrs_for(
    status: str = "running", health: str | None = None, restarts: int = 0, healthcheck: bool = False
) -> dict[str, Any]:
    state: dict[str, Any] = {"Status": status}
    if health:
        state["Health"] = {"Status": health}
    cfg: dict[str, Any] = {"Healthcheck": {"Test": ["CMD", "true"]}} if healthcheck else {}
    return {"State": state, "RestartCount": restarts, "Config": cfg}


async def test_wait_healthy_with_healthcheck_becomes_healthy() -> None:
    clock = Clock()
    client = state_client(
        [attrs_for(health="starting"), attrs_for(health="starting"), attrs_for(health="healthy")]
    )
    assert await make_engine(client, clock).wait_healthy("c", timeout_s=90) is True
    assert clock.sleeps == [2.0, 2.0]


async def test_wait_healthy_unhealthy_fails_fast() -> None:
    clock = Clock()
    client = state_client([attrs_for(health="starting"), attrs_for(health="unhealthy")])
    assert await make_engine(client, clock).wait_healthy("c") is False
    assert clock.t == 2.0


async def test_wait_healthy_healthcheck_timeout() -> None:
    clock = Clock()
    client = state_client([attrs_for(health="starting")])
    assert await make_engine(client, clock).wait_healthy("c", timeout_s=10) is False
    assert clock.t >= 10


async def test_wait_healthy_no_healthcheck_requires_stable_running() -> None:
    clock = Clock()
    client = state_client([attrs_for()])
    assert await make_engine(client, clock).wait_healthy("c", timeout_s=90, stable_s=15) is True
    assert 15 <= clock.t < 18


async def test_wait_healthy_restart_resets_stability_window() -> None:
    clock = Clock()
    # restarts at t=0..6 (count 0 then 1 at t=6), so stable window restarts at t=6
    client = state_client([attrs_for(restarts=0)] * 3 + [attrs_for(restarts=1)])
    assert await make_engine(client, clock).wait_healthy("c", timeout_s=90, stable_s=10) is True
    assert clock.t >= 6 + 10


async def test_wait_healthy_not_running_or_missing_fails() -> None:
    assert await make_engine(state_client([attrs_for(status="exited")]), Clock()).wait_healthy("c") is False
    assert (
        await make_engine(state_client([attrs_for(status="restarting")]), Clock()).wait_healthy("c") is False
    )
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("x")
    assert await make_engine(client, Clock()).wait_healthy("c") is False


async def test_wait_healthy_no_healthcheck_timeout_before_stable() -> None:
    clock = Clock()
    assert (
        await make_engine(state_client([attrs_for()]), clock).wait_healthy("c", timeout_s=6, stable_s=30)
        is False
    )


# ---------- host_ip ----------
ROUTE = (
    "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
    "br0\t00000000\t0100A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "br0\t0000A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
    "docker0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
)
FIB = """Main:
  +-- 0.0.0.0/0 3 0 5
     |-- 0.0.0.0
        /0 universe UNICAST
     +-- 127.0.0.0/8 2 0 2
        |-- 127.0.0.1
           /32 host LOCAL
     +-- 172.17.0.0/16 2 0 2
        |-- 172.17.0.1
           /32 host LOCAL
     +-- 192.168.0.0/24 2 0 2
        |-- 192.168.0.50
           /32 host LOCAL
"""


def proc_tree(tmp_path: Path, route: str | None, fib: str | None) -> str:
    net = tmp_path / "net"
    net.mkdir()
    if route is not None:
        (net / "route").write_text(route)
    if fib is not None:
        (net / "fib_trie").write_text(fib)
    return str(tmp_path)


async def test_host_ip_from_default_route_interface(tmp_path: Path) -> None:
    engine = make_engine(proc_root=proc_tree(tmp_path, ROUTE, FIB))
    assert await engine.host_ip() == "192.168.0.50"


async def test_host_ip_fib_only_skips_loopback_and_docker(tmp_path: Path) -> None:
    engine = make_engine(proc_root=proc_tree(tmp_path, None, FIB))
    assert await engine.host_ip() == "192.168.0.50"


async def test_host_ip_falls_back_to_docker0_gateway(tmp_path: Path) -> None:
    client = MagicMock()
    net = MagicMock()
    net.attrs = {"IPAM": {"Config": [{"Subnet": "172.17.0.0/16", "Gateway": "172.17.0.1"}]}}
    client.networks.list.return_value = [net]
    engine = make_engine(client, proc_root=str(tmp_path / "missing"))
    assert await engine.host_ip() == "172.17.0.1"


# ---------- real Docker ----------
@pytest.mark.docker
async def test_real_docker_smoke() -> None:
    engine = SdkDockerEngine()
    digest = await engine.pull("alpine:3.20", None)
    assert digest.startswith("sha256:")
    name = "uam-smoke-test"
    existing = await engine.inspect(name)
    if existing:
        await engine.remove(existing.id, force=True)
    cid = await engine.create(
        ContainerSpec(
            name=name,
            image="alpine:3.20",
            command=("sleep", "60"),
            restart_policy="no",
            env={"UAM_SMOKE": "1"},
        )
    )
    try:
        await engine.start(cid)
        assert await engine.wait_healthy(cid, timeout_s=30, stable_s=3) is True
        info = await engine.inspect(name)
        assert info is not None and info.state == "running"
        assert info.image_digest == digest
        assert info.env.get("UAM_SMOKE") == "1"
        assert await engine.host_ip()
    finally:
        await engine.remove(cid, force=True)
    assert await engine.inspect(name) is None


async def test_pull_retries_anonymously_when_credential_is_refused() -> None:
    client = MagicMock()
    ok = MagicMock()
    ok.attrs = {"RepoDigests": ["ghcr.io/me/app@sha256:" + "d" * 64]}
    client.images.pull.side_effect = [api_error(500, "error from registry: denied"), ok]
    auth = RegistryAuth(username="u", password="bad", registry="ghcr.io")
    assert await make_engine(client).pull("ghcr.io/me/app:latest", auth) == "sha256:" + "d" * 64
    assert client.images.pull.call_args_list[1].args == ("ghcr.io/me/app:latest",)
    assert client.images.pull.call_args_list[1].kwargs == {}


async def test_pull_auth_error_kept_when_anonymous_also_fails() -> None:
    client = MagicMock()
    client.images.pull.side_effect = [api_error(403), api_error(403)]
    auth = RegistryAuth(username="u", password="bad", registry="ghcr.io")
    with pytest.raises(AuthError):
        await make_engine(client).pull("ghcr.io/me/app:latest", auth)
