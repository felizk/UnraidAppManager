"""Recreate a container on a new image, the way `docker run` would with the same options.

This runs inside the short-lived helper container (see `uam.self_update.__main__`), because UAM
can't stop its own container and then keep working. Everything here is synchronous: the helper is
its own process with nothing else to do.
"""

from __future__ import annotations

import copy
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

OLD_SUFFIX = "__uam_old"
HEALTH_TIMEOUT_S = 180
STABLE_S = 15
POLL_S = 2.0
# Keys of Config that come from the image unless the user set them; copied only when they differ.
_IMAGE_DEFAULT_KEYS = ("Cmd", "Entrypoint", "Healthcheck", "WorkingDir", "User", "StopSignal", "Shell")
# Endpoint settings that describe what the user asked for (the rest is runtime state).
_ENDPOINT_KEYS = ("IPAMConfig", "Links", "Aliases", "DriverOpts", "MacAddress")
_NO_ENDPOINTS = ("host", "none")
_OCI_PREFIX = "org.opencontainers.image."


class EngineApi(Protocol):
    """The raw Docker Engine calls the recreate needs (inspect output is the API's JSON)."""

    def inspect_container(self, ref: str) -> dict[str, Any] | None: ...

    def inspect_image(self, ref: str) -> dict[str, Any] | None: ...

    def create(self, config: dict[str, Any], name: str) -> str: ...

    def connect(self, network: str, container_id: str, endpoint: dict[str, Any]) -> None: ...

    def start(self, container_id: str) -> None: ...

    def stop(self, container_id: str, timeout: int) -> None: ...

    def remove(self, container_id: str, force: bool = False) -> None: ...

    def rename(self, container_id: str, name: str) -> None: ...


@dataclass
class CloneConfig:
    """A create request for the new container, plus networks to connect after it is created."""

    config: dict[str, Any]
    extra_networks: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


def _without_image_defaults(cfg: dict[str, Any], image_cfg: dict[str, Any]) -> None:
    """Drop values the old image supplied, so the new image's own defaults take effect."""
    image_env = set(image_cfg.get("Env") or [])
    env = [e for e in cfg.get("Env") or [] if e not in image_env]
    cfg["Env"] = env
    image_labels = image_cfg.get("Labels") or {}
    cfg["Labels"] = {
        k: v for k, v in (cfg.get("Labels") or {}).items() if k not in image_labels or image_labels[k] != v
    }
    for key in _IMAGE_DEFAULT_KEYS:
        if key in cfg and cfg[key] == image_cfg.get(key):
            del cfg[key]
    for key in ("ExposedPorts", "Volumes"):
        image_set = image_cfg.get(key) or {}
        kept = {k: v for k, v in (cfg.get(key) or {}).items() if k not in image_set}
        if kept:
            cfg[key] = kept
        else:
            cfg.pop(key, None)


def _endpoint(name: str, settings: dict[str, Any], short_id: str) -> dict[str, Any]:
    out = {k: settings[k] for k in _ENDPOINT_KEYS if settings.get(k)}
    aliases = [a for a in out.get("Aliases") or [] if a != short_id]
    if aliases and name != "bridge":  # the default bridge network doesn't allow aliases
        out["Aliases"] = aliases
    else:
        out.pop("Aliases", None)
    return out


def clone_config(container: dict[str, Any], old_image: dict[str, Any], image_ref: str) -> CloneConfig:
    """Build the create request for a copy of `container` that runs `image_ref`.

    Everything the user configured (env, labels, mounts, ports, networks with static IPs, restart
    policy, …) is kept; values that only came from the old image are dropped.
    """
    cfg = copy.deepcopy(container.get("Config") or {})
    if old_image:
        _without_image_defaults(cfg, old_image.get("Config") or {})
    else:
        # The old image is gone, so its defaults can't be told apart; at least don't carry its
        # build labels (version, revision, …) over onto the new image.
        cfg["Labels"] = {k: v for k, v in (cfg.get("Labels") or {}).items() if not k.startswith(_OCI_PREFIX)}
    cfg["Image"] = image_ref
    short_id = str(container.get("Id", ""))[:12]
    if cfg.get("Hostname") == short_id:
        del cfg["Hostname"]  # Docker's generated hostname; let the new container get its own
    host_config = copy.deepcopy(container.get("HostConfig") or {})
    cfg["HostConfig"] = host_config
    mode = str(host_config.get("NetworkMode") or "default")
    networks: dict[str, Any] = dict((container.get("NetworkSettings") or {}).get("Networks") or {})
    if mode in _NO_ENDPOINTS or mode.startswith("container:") or not networks:
        return CloneConfig(cfg)
    primary = "bridge" if mode == "default" else mode
    if primary not in networks:
        primary = next(iter(networks))
    cfg["NetworkingConfig"] = {
        "EndpointsConfig": {primary: _endpoint(primary, networks[primary] or {}, short_id)}
    }
    extras = [
        (name, _endpoint(name, settings or {}, short_id))
        for name, settings in networks.items()
        if name != primary
    ]
    return CloneConfig(cfg, extras)


@dataclass
class RecreateResult:
    """What the helper did; written to the data folder for the restarted UAM to report."""

    ok: bool
    message: str
    old_image: str = ""
    new_image: str = ""
    rolled_back: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "ok": self.ok,
            "message": self.message,
            "old_image": self.old_image,
            "new_image": self.new_image,
            "rolled_back": self.rolled_back,
        }


def wait_healthy(
    api: EngineApi,
    container_id: str,
    timeout_s: float = HEALTH_TIMEOUT_S,
    stable_s: float = STABLE_S,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """True once the healthcheck passes, or (without one) the container has run for `stable_s`.

    A container that exits, restarts or turns unhealthy fails at once.
    """
    start = monotonic()
    while True:
        attrs = api.inspect_container(container_id)
        if attrs is None:
            return False
        state = attrs.get("State") or {}
        health = (state.get("Health") or {}).get("Status")
        if state.get("Status") in ("exited", "dead", "restarting") or health == "unhealthy":
            return False
        if int(attrs.get("RestartCount") or 0) > 0:
            return False  # it crashed and the restart policy brought it back
        if health == "healthy":
            return True
        test = ((attrs.get("Config") or {}).get("Healthcheck") or {}).get("Test") or []
        has_check = bool(health) or (bool(test) and list(test) != ["NONE"])
        now = monotonic()
        if not has_check and state.get("Status") == "running" and now - start >= stable_s:
            return True
        if now - start >= timeout_s:
            return False
        sleep(POLL_S)


def recreate(
    api: EngineApi,
    target: str,
    image_ref: str,
    *,
    health_timeout_s: float = HEALTH_TIMEOUT_S,
    stable_s: float = STABLE_S,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> RecreateResult:
    """Replace container `target` with one running `image_ref`; restore the old one on any failure.

    Order: stop old, rename it to `<name>__uam_old`, create and start the new one, wait until it is
    healthy, then remove the old one. The old container is stopped first because it holds the
    host ports and any static IP the new one needs.
    """
    old = api.inspect_container(target)
    if old is None:
        return RecreateResult(False, f"container {target} not found")
    name = str(old.get("Name", "")).lstrip("/")
    old_image_id = str(old.get("Image", ""))
    old_image = api.inspect_image(old_image_id) or {}
    new_image = api.inspect_image(image_ref)
    if new_image is None:
        return RecreateResult(False, f"image {image_ref} is not on the server", old_image=old_image_id)
    new_image_id = str(new_image.get("Id", ""))
    result = RecreateResult(False, "", old_image=old_image_id, new_image=new_image_id)
    if new_image_id == old_image_id:
        result.ok, result.message = True, "already running the newest image"
        return result
    clone = clone_config(old, old_image, image_ref)
    old_id = str(old["Id"])
    was_running = (old.get("State") or {}).get("Status") == "running"
    new_id: str | None = None
    stage = "stop old container"
    try:
        api.stop(old_id, timeout=30)
        stage = "rename old container"
        api.rename(old_id, name + OLD_SUFFIX)
        stage = "create new container"
        new_id = api.create(clone.config, name)
        stage = "connect networks"
        for net, endpoint in clone.extra_networks:
            api.connect(net, new_id, endpoint)
        stage = "start new container"
        api.start(new_id)
        stage = "health check"
        if not wait_healthy(api, new_id, health_timeout_s, stable_s, sleep, monotonic):
            raise RuntimeError("the new container did not become healthy")
    except Exception as exc:  # noqa: BLE001 - every failure leads to the same rollback
        log.error("%s failed: %s; restoring the old container", stage, exc)
        result.message = f"{stage} failed: {exc}"
        result.rolled_back = True
        restore_error = _restore(api, old_id, name, new_id, was_running)
        if restore_error:
            result.message += f"; restoring the old container also failed: {restore_error}"
        return result
    try:
        api.remove(old_id, force=True)
    except Exception as exc:  # noqa: BLE001 - the update itself worked
        log.warning("could not remove the old container: %s", exc)
    result.ok, result.message = True, "updated"
    return result


def _restore(api: EngineApi, old_id: str, name: str, new_id: str | None, start: bool) -> str:
    """Put the old container back under its name (and running). Returns an error text or ''."""
    errors: list[str] = []
    if new_id is not None:
        try:
            api.remove(new_id, force=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"remove new: {exc}")
    current = api.inspect_container(old_id)
    if current is not None and str(current.get("Name", "")).lstrip("/") != name:
        try:
            api.rename(old_id, name)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"rename: {exc}")
    if start:
        try:
            api.start(old_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"start: {exc}")
    return "; ".join(errors)
