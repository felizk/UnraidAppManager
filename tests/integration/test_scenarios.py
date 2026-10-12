"""End-to-end scenarios 1-8 from docs/tasks/T15: real Docker, real NPM, local registry, GitHub stub.

Every test builds its own repo, image and app names (prefix `uamtest-`) so scenarios are independent.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import docker
import docker.errors
import pytest

from tests.integration.stub_github import StubGitHub
from tests.integration.support import (
    BASE_DOMAIN,
    PORT_RANGE,
    LocalRegistry,
    NpmApi,
    Uam,
    config,
    free_port,
    template_xml,
    wait_for,
)

pytestmark = pytest.mark.integration

GOLDEN = Path(__file__).parent / "golden" / "my-uamtest-notes.xml"


# --------------------------------------------------------------------------- helpers
class Scenario:
    """The repo, image and template of one scenario."""

    def __init__(self, name: str, github: StubGitHub, registry: LocalRegistry) -> None:
        self.name = f"uamtest-{name}"
        self.repo = name
        self.slug = f"uamtest/{name}"
        self.image = registry.image(self.name)
        self.template_path = f"unraid/{self.name}.xml"
        self.github = github
        self.registry = registry

    def publish(self, **kwargs: Any) -> str:
        """Commit the template (kwargs go to `template_xml`) and return the commit sha."""
        kwargs.setdefault("repo", self.repo)
        xml = template_xml(self.name, self.image, **kwargs)
        return self.github.commit(self.slug, {self.template_path: xml})

    def push(self, **kwargs: Any) -> str:
        """Build and push an image of this scenario; returns its manifest digest."""
        kwargs.setdefault("source_repo", self.repo)
        return self.registry.build_and_push(self.name, **kwargs)


@pytest.fixture
def make_scenario(github: StubGitHub, registry: LocalRegistry):
    def make(name: str) -> Scenario:
        return Scenario(name, github, registry)

    return make


def containers_named(client: docker.DockerClient, prefix: str) -> list[Any]:
    """All containers (any state) whose name starts with `prefix`."""
    return [c for c in client.containers.list(all=True) if (c.name or "").startswith(prefix)]


def host_port_of(container: Any, container_port: int = 3000) -> int:
    """The host port published for a container port."""
    container.reload()
    return int(container.attrs["NetworkSettings"]["Ports"][f"{container_port}/tcp"][0]["HostPort"])


def health_of(container: Any) -> str | None:
    """The container's current healthcheck status."""
    container.reload()
    status = container.attrs["State"].get("Health", {}).get("Status")
    return str(status) if status else None


def body(port: int) -> str:
    """What the app listening on a published host port serves."""
    from tests.integration.support import local_get

    return str(
        wait_for(
            lambda: local_get(f"http://127.0.0.1:{port}/").text.strip(), timeout=30, what=f"app on {port}"
        )
    )


# --------------------------------------------------------------------------- 1
def test_01_add_source_discover_install(
    uam: Uam, npm: NpmApi | None, docker_client: docker.DockerClient, gateway_ip: str, make_scenario
):
    s = make_scenario("s1")
    digest = s.push(version="v1")
    commit = s.publish()
    source_id = uam.add_source(s.repo)

    found = uam.api("GET", f"/sources/{source_id}/templates")
    assert [t["path"] for t in found] == [s.template_path]
    assert found[0]["name"] == s.name and found[0]["parse_error"] is None

    sub = "s1" if npm else None
    app = uam.install(source_id, s.template_path, data_name=s.name, subdomain=sub)

    assert app["installed_image_digest"] == digest
    assert app["installed_template_commit"] == commit
    container = docker_client.containers.get(s.name)
    assert container.status == "running" and container.id == app["container_id"]

    port = host_port_of(container)
    assert PORT_RANGE[0] <= port <= PORT_RANGE[1]
    assert uam.host_ports(app["id"]) == {3000: port}
    assert body(port) == "v1"

    labels = container.labels
    assert labels["uam.managed"] == "true"
    assert labels["uam.app_id"] == str(app["id"])
    assert labels["uam.source"] == s.slug
    assert labels["uam.template_commit"] == commit
    assert labels["net.unraid.docker.managed"] == "uam"
    assert labels["net.unraid.docker.webui"] == f"http://[IP]:{port}/"

    if npm is None:
        pytest.skip("NPM did not come up; container assertions passed, proxy assertions skipped")
    domain = f"s1.{BASE_DOMAIN}"
    hosts = npm.hosts_for(domain)
    assert len(hosts) == 1
    assert (hosts[0]["forward_host"], hosts[0]["forward_port"]) == (gateway_ip, port)
    assert "managed-by: unraid-app-manager" in hosts[0]["advanced_config"]
    assert npm.body_via(domain, "v1") == "v1"


# --------------------------------------------------------------------------- 2
def test_02_image_update_is_detected_and_applied(uam: Uam, docker_client: docker.DockerClient, make_scenario):
    s = make_scenario("s2")
    old_digest = s.push(version="v1")
    s.publish()
    app = uam.install(uam.add_source(s.repo), s.template_path, data_name=s.name)
    old_id = app["container_id"]
    assert uam.check(app["id"])["image_changed"] is False

    new_digest = s.push(version="v2")
    assert new_digest != old_digest
    result = uam.check(app["id"])
    assert result["image_changed"] is True and result["new_image_digest"] == new_digest

    app = uam.settle(app["id"])
    assert app["state"] == "running", app["last_error"]
    assert app["installed_image_digest"] == new_digest
    assert app["available_image_digest"] is None
    assert app["container_id"] != old_id

    with pytest.raises(docker.errors.NotFound):
        docker_client.containers.get(old_id)
    assert [c.name for c in containers_named(docker_client, s.name)] == [s.name]
    container = docker_client.containers.get(s.name)
    assert container.id == app["container_id"] and container.status == "running"
    assert body(host_port_of(container)) == "v2"
    kinds = {e["kind"] for e in uam.api("GET", f"/apps/{app['id']}/events")}
    assert {"auto_update", "update.cleanup_old"} <= kinds


# --------------------------------------------------------------------------- 3
def test_03_unhealthy_update_rolls_back(uam: Uam, docker_client: docker.DockerClient, make_scenario):
    s = make_scenario("s3")
    old_digest = s.push(version="v1")
    s.publish()
    app = uam.install(uam.add_source(s.repo), s.template_path, data_name=s.name)
    old_id = app["container_id"]
    port = host_port_of(docker_client.containers.get(old_id))

    bad_digest = s.push(version="v2", healthy=False)
    result = uam.check(app["id"])
    assert result["image_changed"] is True and result["new_image_digest"] == bad_digest

    app = uam.settle(app["id"])
    assert app["state"] == "failed_update"
    assert "health" in (app["last_error"] or "")
    assert app["installed_image_digest"] == old_digest
    assert app["container_id"] == old_id

    assert [c.name for c in containers_named(docker_client, s.name)] == [s.name]
    old = docker_client.containers.get(s.name)
    assert old.id == old_id and old.status == "running"
    assert host_port_of(old) == port
    assert body(port) == "v1"
    kinds = {e["kind"] for e in uam.api("GET", f"/apps/{app['id']}/events")}
    assert "update.failed" in kinds


# --------------------------------------------------------------------------- 4
def test_04_new_required_variable_needs_attention(
    uam: Uam, docker_client: docker.DockerClient, make_scenario
):
    s = make_scenario("s4")
    s.push(version="v1")
    first = s.publish()
    app = uam.install(uam.add_source(s.repo), s.template_path, data_name=s.name)
    # Even with template auto-update on, a required value without a default must block the update.
    uam.api("PATCH", f"/apps/{app['id']}", json={"auto_update_template": True})
    old_id = app["container_id"]

    required = config("API key", "API_KEY", "Variable", required=True)
    second = s.publish(extra_configs=required)
    result = uam.check(app["id"])

    assert result["template_changed"] is True and result["new_template_commit"] == second
    assert "Variable:API_KEY" in (result["needs_attention"] or "")
    after = uam.settle(app["id"])
    assert after["state"] == "running"
    assert after["needs_attention"] and "Variable:API_KEY" in after["needs_attention"]
    assert after["available_template_commit"] == second
    assert after["installed_template_commit"] == first
    assert after["container_id"] == old_id
    assert docker_client.containers.get(s.name).id == old_id
    assert "auto_update" not in {e["kind"] for e in uam.api("GET", f"/apps/{app['id']}/events")}


# --------------------------------------------------------------------------- 5
def test_05_uninstall_removes_everything(
    uam: Uam, npm: NpmApi | None, docker_client: docker.DockerClient, make_scenario
):
    s = make_scenario("s5")
    s.push(version="v1")
    s.publish()
    sub = "s5" if npm else None
    app = uam.install(uam.add_source(s.repo), s.template_path, data_name=s.name, subdomain=sub)
    port = uam.host_ports(app["id"])[3000]
    if npm is not None:
        assert len(npm.hosts_for(f"s5.{BASE_DOMAIN}")) == 1

    uam.api("DELETE", f"/apps/{app['id']}", expect=204)

    assert containers_named(docker_client, s.name) == []
    assert uam.api("GET", f"/apps/{app['id']}", expect=404) is not None
    ports = uam.api("GET", "/ports")
    assert [a for a in ports["allocations"] if a["app_id"] == app["id"]] == []
    assert port not in {p["host_port"] for p in ports["in_use"]}
    if npm is None:
        pytest.skip("NPM did not come up; container and port assertions passed")
    assert npm.hosts_for(f"s5.{BASE_DOMAIN}") == []


# --------------------------------------------------------------------------- 6
def _normalise(xml: str, s: Scenario, port: int, data_dir: str) -> str:
    xml = xml.replace(s.image, "{{IMAGE}}").replace(data_dir, "{{DATA}}").replace(s.name, "{{NAME}}")
    xml = xml.replace(s.repo + "/", "{{REPO}}/").replace(s.repo + "<", "{{REPO}}<")
    return re.sub(rf"(?<=>){port}(?=<)", "{{PORT}}", xml)


def test_06_template_export_matches_golden(uam: Uam, make_scenario):
    s = make_scenario("s6")
    s.push(version="v1")
    s.publish()
    app = uam.install(
        uam.add_source(s.repo), s.template_path, data_name=s.name, overrides={"Variable:TZ": "Europe/Berlin"}
    )
    port = uam.host_ports(app["id"])[3000]
    data_dir = str(uam.appdata / s.name)

    plans = uam.api("POST", "/templates/export/plan", json={"app_ids": [app["id"]]})
    assert len(plans) == 1 and plans[0]["changed"] and plans[0]["old_xml"] is None
    written = uam.api("POST", "/templates/export/apply", json={"plans": plans})["written"]

    target = uam.flash / f"my-{s.name}.xml"
    assert written == [str(target)] and target.is_file()
    actual = _normalise(target.read_text(), s, port, data_dir)
    if os.environ.get("UAM_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(actual)
    assert actual == GOLDEN.read_text()
    # A second plan finds nothing to change.
    again = uam.api("POST", "/templates/export/plan", json={"app_ids": [app["id"]]})
    assert again[0]["changed"] is False


# --------------------------------------------------------------------------- 7
def test_07_import_existing_container(
    uam: Uam, npm: NpmApi | None, docker_client: docker.DockerClient, gateway_ip: str, make_scenario
):
    s = make_scenario("s7")
    digest = s.push(version="v1")
    commit = s.publish(extra_configs=config("Greeting", "GREETING", "Variable"))
    data_dir = uam.data_dir(s.name)

    # What the Unraid Docker tab would have left behind: a dockerMan container and its flash template.
    host_port = free_port()
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "run",
            "-d",
            "--name",
            s.name,
            "--label",
            "net.unraid.docker.managed=dockerman",
            "-p",
            f"{host_port}:3000",
            "-e",
            "TZ=Europe/Berlin",
            "-e",
            "GREETING=hello",
            "-v",
            f"{data_dir}:/data",
            "--restart",
            "unless-stopped",
            s.image,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    old = docker_client.containers.get(s.name)
    wait_for(
        lambda: health_of(old) == "healthy",
        timeout=30,
        what="old container",
    )
    old_image_id = old.attrs["Image"]
    old_env = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in old.attrs["Config"]["Env"]}
    flash = template_xml(
        s.name,
        s.image,
        repo=s.repo,
        port_value=str(host_port),
        tz_value="Europe/Berlin",
        data_path=data_dir,
        extra_configs=config("Greeting", "GREETING", "Variable", value="hello"),
    )
    (uam.flash / f"my-{s.name}.xml").write_text(flash)

    domain = f"imported-s7.{BASE_DOMAIN}"
    manual_host = npm.create_host(domain, gateway_ip, host_port) if npm else None

    source_id = uam.add_source(s.repo)
    candidates = {c["container_name"]: c for c in uam.api("GET", "/imports/candidates")}
    cand = candidates[s.name]
    assert cand["unraid_managed"] == "dockerman" and cand["blockers"] == []
    assert cand["flash_template_path"] == str(uam.flash / f"my-{s.name}.xml")
    assert cand["source_matches"][0]["source_id"] == source_id
    assert cand["source_matches"][0]["template_path"] == s.template_path
    if npm:
        assert cand["proxy_host"]["id"] == manual_host

    plan = uam.api("POST", "/imports/plan", json={"container_name": s.name, "source_id": source_id})
    assert plan["template_path"] == s.template_path and plan["template_commit"] == commit
    assert plan["installed_image_digest"] == digest
    assert plan["pinned_ports"] == [[3000, "tcp", host_port]]
    assert plan["overrides"]["Variable:TZ"] == "Europe/Berlin"
    assert plan["overrides"]["Variable:GREETING"] == "hello"
    assert plan["adopt_proxy_host_id"] == manual_host

    accepted = uam.api("POST", "/imports/apply", json=plan)
    app = uam.settle(accepted["app_id"])
    assert app["state"] == "running", app["last_error"]
    assert app["needs_attention"] is None and app["labels_pending"] is False
    assert app["installed_image_digest"] == digest

    new = docker_client.containers.get(s.name)
    assert new.id != old.id and new.id == app["container_id"] and new.status == "running"
    with pytest.raises(docker.errors.NotFound):
        docker_client.containers.get(str(old.id))
    assert [c.name for c in containers_named(docker_client, s.name)] == [s.name]
    assert new.labels["uam.managed"] == "true" and new.labels["uam.app_id"] == str(app["id"])
    assert new.labels["net.unraid.docker.managed"] == "uam"
    assert new.attrs["Image"] == old_image_id
    assert host_port_of(new) == host_port
    assert uam.host_ports(app["id"]) == {3000: host_port}
    new_env = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in new.attrs["Config"]["Env"]}
    assert new_env == old_env
    assert body(host_port) == "v1"

    if npm is None:
        pytest.skip("NPM did not come up; container assertions passed, proxy assertions skipped")
    hosts = npm.hosts_for(domain)
    assert [h["id"] for h in hosts] == [manual_host]
    assert len(npm.hosts()) == 1
    assert "managed-by: unraid-app-manager" in hosts[0]["advanced_config"]
    assert app["proxy"]["subdomain"] == "imported-s7"
    assert npm.body_via(domain, "v1") == "v1"


# --------------------------------------------------------------------------- 8
def test_08_same_preferred_port_gets_different_host_ports(
    uam: Uam, docker_client: docker.DockerClient, github, registry
):
    a, b = Scenario("s8a", github, registry), Scenario("s8b", github, registry)
    a.push(version="v1")
    b.push(version="v1")
    preferred = str(PORT_RANGE[0] + 50)
    a.publish(port=preferred)
    b.publish(port=preferred)

    app_a = uam.install(uam.add_source(a.repo), a.template_path, data_name=a.name)
    app_b = uam.install(uam.add_source(b.repo), b.template_path, data_name=b.name)

    port_a, port_b = uam.host_ports(app_a["id"])[3000], uam.host_ports(app_b["id"])[3000]
    assert port_a == int(preferred)
    assert port_b != port_a and PORT_RANGE[0] <= port_b <= PORT_RANGE[1]
    assert host_port_of(docker_client.containers.get(a.name)) == port_a
    assert host_port_of(docker_client.containers.get(b.name)) == port_b
    assert body(port_a) == body(port_b) == "v1"
