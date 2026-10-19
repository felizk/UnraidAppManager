from __future__ import annotations

import copy
import itertools
from typing import Any

import pytest

from uam.self_update.recreate import OLD_SUFFIX, RecreateResult, clone_config, recreate, wait_healthy

OLD_ID = "a" * 64
OLD_IMAGE = "sha256:" + "1" * 64
NEW_IMAGE = "sha256:" + "2" * 64
REF = "ghcr.io/felizk/unraid-app-manager:latest"

IMAGE_CONFIG: dict[str, Any] = {
    "Env": ["PATH=/app/.venv/bin:/usr/bin", "UAM_PORT=8484"],
    "Cmd": ["python", "-m", "uam"],
    "Labels": {"org.opencontainers.image.revision": "old", "org.opencontainers.image.title": "UAM"},
    "ExposedPorts": {"8484/tcp": {}},
    "Volumes": {"/data": {}},
    "WorkingDir": "/app",
    "Healthcheck": {"Test": ["CMD-SHELL", "true"]},
}


def container(**overrides: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "Id": OLD_ID,
        "Name": "/unraid-app-manager",
        "Image": OLD_IMAGE,
        "RestartCount": 0,
        "State": {"Status": "running"},
        "Config": {
            "Hostname": OLD_ID[:12],
            "Image": REF,
            "Env": [*IMAGE_CONFIG["Env"], "UAM_API_TOKEN=x", "TZ=Europe/Copenhagen"],
            "Cmd": IMAGE_CONFIG["Cmd"],
            "WorkingDir": "/app",
            "Healthcheck": IMAGE_CONFIG["Healthcheck"],
            "Labels": {
                **IMAGE_CONFIG["Labels"],
                "net.unraid.docker.managed": "dockerman",
                "net.unraid.docker.icon": "https://example.com/icon.png",
            },
            "ExposedPorts": {"8484/tcp": {}, "9000/tcp": {}},
            "Volumes": {"/data": {}},
        },
        "HostConfig": {
            "Binds": ["/var/run/docker.sock:/var/run/docker.sock", "/mnt/user/appdata/uam:/data:rw"],
            "NetworkMode": "br0",
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "PortBindings": {},
        },
        "NetworkSettings": {
            "Networks": {
                "br0": {
                    "IPAMConfig": {"IPv4Address": "192.168.1.50"},
                    "Aliases": [OLD_ID[:12], "uam"],
                    "MacAddress": "02:42:c0:a8:01:32",
                    "NetworkID": "n1",
                    "EndpointID": "e1",
                    "IPAddress": "192.168.1.50",
                    "DNSNames": ["unraid-app-manager"],
                },
                "proxynet": {"Aliases": ["uam-proxy"], "NetworkID": "n2", "IPAddress": "172.18.0.5"},
            }
        },
    }
    attrs.update(overrides)
    return attrs


def test_clone_keeps_user_settings_and_drops_image_defaults() -> None:
    clone = clone_config(container(), {"Config": IMAGE_CONFIG}, REF)
    cfg = clone.config
    assert cfg["Image"] == REF
    assert cfg["Env"] == ["UAM_API_TOKEN=x", "TZ=Europe/Copenhagen"]
    assert cfg["Labels"] == {
        "net.unraid.docker.managed": "dockerman",
        "net.unraid.docker.icon": "https://example.com/icon.png",
    }
    for key in ("Cmd", "WorkingDir", "Healthcheck", "Volumes", "Hostname"):
        assert key not in cfg
    assert cfg["ExposedPorts"] == {"9000/tcp": {}}
    assert cfg["HostConfig"]["Binds"][1] == "/mnt/user/appdata/uam:/data:rw"
    assert cfg["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
    assert cfg["NetworkingConfig"] == {
        "EndpointsConfig": {
            "br0": {
                "IPAMConfig": {"IPv4Address": "192.168.1.50"},
                "Aliases": ["uam"],
                "MacAddress": "02:42:c0:a8:01:32",
            }
        }
    }
    assert clone.extra_networks == [("proxynet", {"Aliases": ["uam-proxy"]})]


def test_clone_keeps_overridden_image_values_and_custom_hostname() -> None:
    attrs = container()
    attrs["Config"]["Cmd"] = ["python", "-m", "uam", "--debug"]
    attrs["Config"]["Hostname"] = "uam-box"
    attrs["Config"]["Labels"]["org.opencontainers.image.title"] = "My UAM"
    cfg = clone_config(attrs, {"Config": IMAGE_CONFIG}, REF).config
    assert cfg["Cmd"] == ["python", "-m", "uam", "--debug"]
    assert cfg["Hostname"] == "uam-box"
    assert cfg["Labels"]["org.opencontainers.image.title"] == "My UAM"


def test_clone_does_not_modify_the_input() -> None:
    attrs = container()
    before = copy.deepcopy(attrs)
    clone_config(attrs, {"Config": IMAGE_CONFIG}, REF)
    assert attrs == before


@pytest.mark.parametrize("mode", ["host", "none", "container:abc"])
def test_clone_without_endpoints(mode: str) -> None:
    attrs = container()
    attrs["HostConfig"]["NetworkMode"] = mode
    clone = clone_config(attrs, {"Config": IMAGE_CONFIG}, REF)
    assert "NetworkingConfig" not in clone.config and clone.extra_networks == []


def test_clone_default_bridge_has_no_aliases() -> None:
    attrs = container()
    attrs["HostConfig"]["NetworkMode"] = "default"
    attrs["NetworkSettings"]["Networks"] = {"bridge": {"Aliases": ["x"], "IPAddress": "172.17.0.2"}}
    clone = clone_config(attrs, {"Config": IMAGE_CONFIG}, REF)
    assert clone.config["NetworkingConfig"] == {"EndpointsConfig": {"bridge": {}}}


def test_clone_with_missing_old_image_drops_build_labels() -> None:
    cfg = clone_config(container(), {}, REF).config
    assert not any(k.startswith("org.opencontainers.image.") for k in cfg["Labels"])
    assert cfg["Labels"]["net.unraid.docker.managed"] == "dockerman"


class FakeEngine:
    """Containers keyed by id; `health` is the status sequence the new container reports."""

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {OLD_ID: container()}
        self.images: dict[str, dict[str, Any]] = {
            OLD_IMAGE: {"Id": OLD_IMAGE, "Config": IMAGE_CONFIG},
            REF: {"Id": NEW_IMAGE, "Config": IMAGE_CONFIG},
        }
        self.calls: list[tuple[Any, ...]] = []
        self.fail: dict[str, Exception] = {}
        self.health: list[str] = ["starting", "healthy"]
        self._ids = itertools.count(1)

    def _check(self, op: str) -> None:
        if op in self.fail:
            raise self.fail.pop(op)

    def _find(self, ref: str) -> dict[str, Any] | None:
        if ref in self.containers:
            return self.containers[ref]
        return next((c for c in self.containers.values() if c["Name"].lstrip("/") == ref), None)

    def inspect_container(self, ref: str) -> dict[str, Any] | None:
        found = self._find(ref)
        if found is not None and found["Id"] != OLD_ID and found["State"]["Status"] == "running":
            status = self.health.pop(0) if len(self.health) > 1 else self.health[0]
            found["State"]["Health"] = {"Status": status}
        return copy.deepcopy(found) if found else None

    def inspect_image(self, ref: str) -> dict[str, Any] | None:
        return self.images.get(ref)

    def create(self, config: dict[str, Any], name: str) -> str:
        self.calls.append(("create", name))
        self._check("create")
        cid = f"new{next(self._ids)}"
        self.containers[cid] = {
            "Id": cid,
            "Name": "/" + name,
            "Config": config,
            "State": {"Status": "created"},
        }
        return cid

    def connect(self, network: str, container_id: str, endpoint: dict[str, Any]) -> None:
        self.calls.append(("connect", network, container_id))
        self._check("connect")

    def start(self, container_id: str) -> None:
        self.calls.append(("start", container_id))
        self._check("start")
        self.containers[container_id]["State"] = {"Status": "running"}

    def stop(self, container_id: str, timeout: int) -> None:
        self.calls.append(("stop", container_id))
        self._check("stop")
        self.containers[container_id]["State"] = {"Status": "exited"}

    def remove(self, container_id: str, force: bool = False) -> None:
        self.calls.append(("remove", container_id))
        self._check("remove")
        del self.containers[container_id]

    def rename(self, container_id: str, name: str) -> None:
        self.calls.append(("rename", container_id, name))
        self._check("rename")
        self.containers[container_id]["Name"] = "/" + name


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def run(engine: FakeEngine, **kwargs: Any) -> RecreateResult:
    clock = Clock()
    return recreate(engine, OLD_ID, REF, sleep=clock.sleep, monotonic=clock.monotonic, **kwargs)


def test_recreate_success() -> None:
    engine = FakeEngine()
    result = run(engine)
    assert result.ok and result.message == "updated" and not result.rolled_back
    assert (result.old_image, result.new_image) == (OLD_IMAGE, NEW_IMAGE)
    assert engine.calls == [
        ("stop", OLD_ID),
        ("rename", OLD_ID, "unraid-app-manager" + OLD_SUFFIX),
        ("create", "unraid-app-manager"),
        ("connect", "proxynet", "new1"),
        ("start", "new1"),
        ("remove", OLD_ID),
    ]
    assert list(engine.containers) == ["new1"]
    assert engine.containers["new1"]["Config"]["Image"] == REF


def test_recreate_rolls_back_when_unhealthy() -> None:
    engine = FakeEngine()
    engine.health = ["starting", "unhealthy"]
    result = run(engine)
    assert not result.ok and result.rolled_back
    assert "health check failed" in result.message
    assert engine.calls[-3:] == [
        ("remove", "new1"),
        ("rename", OLD_ID, "unraid-app-manager"),
        ("start", OLD_ID),
    ]
    assert list(engine.containers) == [OLD_ID]
    assert engine.containers[OLD_ID]["State"]["Status"] == "running"


def test_recreate_rolls_back_when_create_fails() -> None:
    engine = FakeEngine()
    engine.fail["create"] = RuntimeError("port is already allocated")
    result = run(engine)
    assert result.rolled_back and "create new container failed: port is already allocated" in result.message
    assert engine.calls[-2:] == [("rename", OLD_ID, "unraid-app-manager"), ("start", OLD_ID)]


def test_recreate_reports_a_failed_restore() -> None:
    class NoStart(FakeEngine):
        def start(self, container_id: str) -> None:
            self.calls.append(("start", container_id))
            raise RuntimeError("cannot start")

    engine = NoStart()
    result = run(engine)
    assert result.rolled_back
    assert result.message.startswith("start new container failed: cannot start")
    assert "restoring the old container also failed: start: cannot start" in result.message


def test_recreate_same_image_does_nothing() -> None:
    engine = FakeEngine()
    engine.images[REF] = {"Id": OLD_IMAGE, "Config": IMAGE_CONFIG}
    result = run(engine)
    assert result.ok and result.message == "already running the newest image" and engine.calls == []


def test_recreate_missing_target_or_image() -> None:
    engine = FakeEngine()
    assert not recreate(engine, "nope", REF).ok
    del engine.images[REF]
    result = recreate(engine, OLD_ID, REF)
    assert not result.ok and "not on the server" in result.message and engine.calls == []


def test_wait_healthy_without_healthcheck_needs_stable_run() -> None:
    engine = FakeEngine()
    engine.containers["c"] = {"Id": OLD_ID, "Name": "/c", "Config": {}, "State": {"Status": "running"}}
    clock = Clock()
    assert wait_healthy(engine, "c", timeout_s=60, stable_s=10, sleep=clock.sleep, monotonic=clock.monotonic)
    assert clock.now >= 10


def test_wait_healthy_fails_on_restart_or_timeout() -> None:
    engine = FakeEngine()
    engine.containers["c"] = {
        "Id": OLD_ID,
        "Name": "/c",
        "Config": {},
        "State": {"Status": "running"},
        "RestartCount": 1,
    }
    assert not wait_healthy(engine, "c", sleep=lambda s: None)
    engine.containers["c"]["RestartCount"] = 0
    engine.containers["c"]["State"] = {"Status": "running", "Health": {"Status": "starting"}}
    clock = Clock()
    assert not wait_healthy(engine, "c", timeout_s=20, sleep=clock.sleep, monotonic=clock.monotonic)
