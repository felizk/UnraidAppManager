"""UAM tracking and updating its own container.

Checking works like it does for apps: the registry's current digest for UAM's image tag is compared
with the digest of the image UAM is running. Updating can't happen in-process, because stopping
UAM's container would stop the update half-way. So UAM pulls the new image, then starts a small
helper container (from the image it is running now) that stops UAM, recreates it with the same
settings on the new image, waits until it is healthy, and puts the old container back if not.
The helper writes the outcome to the data folder, and the UAM that starts next reports it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from uam.contracts import DockerEngine, EventLog, KeyValueStore, Notifier, RegistryClient, Vault
from uam.errors import ConflictError, ValidationError
from uam.logging import redact
from uam.self_update.recreate import EngineApi

log = logging.getLogger(__name__)

STATE_KEY = "self_update.state"
RESULT_FILE = "self-update-result.json"
DOCKER_SOCKET = "/var/run/docker.sock"
HELPER_DATA_DIR = "/uam-data"
HELPER_SUFFIX = "-self-update"
LABEL_HELPER = "uam.self_update"
UPDATING_STALE_AFTER = timedelta(minutes=15)
LABEL_REVISION = "org.opencontainers.image.revision"
LABEL_CREATED = "org.opencontainers.image.created"
LABEL_VERSION = "org.opencontainers.image.version"


def package_version() -> str:
    """The installed UAM version."""
    try:
        return version("unraid-app-manager")
    except PackageNotFoundError:
        return "0.0.0"


@dataclass
class SelfStatus:
    """What the UI shows about UAM itself."""

    supported: bool
    reason: str = ""
    container_name: str = ""
    image: str = ""
    version: str = field(default_factory=package_version)
    image_version: str = ""
    revision: str = ""
    created: str = ""
    current_digest: str | None = None
    latest_digest: str | None = None
    checked_at: datetime | None = None
    error: str = ""
    updating: bool = False
    last_result: dict[str, Any] | None = None

    @property
    def update_available(self) -> bool:
        """True when the registry has an image other than the one running.

        `current_digest` is None when the running image has no registry digest any more (for example
        after a rollback on Docker's containerd image store); that still counts as different.
        """
        return bool(self.supported and self.latest_digest and self.latest_digest != self.current_digest)


@dataclass
class _Self:
    """UAM's own container, as found on the Docker engine."""

    id: str
    name: str
    image: str
    image_id: str
    digest: str | None
    socket_source: str | None
    data_source: str | None
    labels: dict[str, str]


class SelfUpdater:
    """Checks for and applies updates to UAM's own container."""

    def __init__(
        self,
        engine: EngineApi,
        docker: DockerEngine,
        registry: RegistryClient,
        vault: Vault,
        kv: KeyValueStore,
        events: EventLog,
        clock: Callable[[], datetime],
        *,
        container_ref: str | None,
        data_dir: Path,
        notifier: Notifier | None = None,
        auto_update: bool = False,
    ) -> None:
        self._engine = engine
        self._docker = docker
        self._registry = registry
        self._vault = vault
        self._kv = kv
        self._events = events
        self._clock = clock
        self._ref = container_ref
        self._data_dir = data_dir
        self._notifier = notifier
        self._auto_update = auto_update
        self._tasks: set[asyncio.Task[None]] = set()

    # ----------------------------------------------------------------- state
    async def _load(self) -> dict[str, Any]:
        raw = await self._kv.get(STATE_KEY)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def _save(self, state: dict[str, Any]) -> None:
        await self._kv.set(STATE_KEY, json.dumps(state))

    def _updating(self, state: dict[str, Any]) -> bool:
        since = state.get("updating_since")
        if not since:
            return False
        return self._clock() - datetime.fromisoformat(since) < UPDATING_STALE_AFTER

    # ----------------------------------------------------------------- discovery
    async def _locate(self) -> tuple[_Self | None, str]:
        if not self._ref:
            return None, "UAM isn't running in a Docker container (or can't tell which one it is)"
        try:
            attrs = await asyncio.to_thread(self._engine.inspect_container, self._ref)
        except Exception as exc:  # noqa: BLE001 - no Docker socket and the like
            return None, redact(f"can't reach Docker: {exc}")
        if attrs is None:
            return None, f"UAM's container ({self._ref[:12]}) isn't visible on the Docker socket"
        config = attrs.get("Config") or {}
        image = str(config.get("Image") or "")
        mounts = {m.get("Destination"): m.get("Source") for m in attrs.get("Mounts") or []}
        info = await self._docker.inspect(str(attrs["Id"]))
        return (
            _Self(
                id=str(attrs["Id"]),
                name=str(attrs.get("Name", "")).lstrip("/"),
                image=image,
                image_id=str(attrs.get("Image") or ""),
                digest=info.image_digest if info else None,
                socket_source=mounts.get(DOCKER_SOCKET),
                data_source=mounts.get(str(self._data_dir)),
                labels=dict(info.image_labels) if info else {},
            ),
            "",
        )

    @staticmethod
    def _blocker(me: _Self) -> str:
        if "@" in me.image:
            return "UAM's image is pinned to a digest; change the tag in Unraid to update it"
        if me.image.startswith("sha256:") or not me.image:
            return "UAM was started from an image id, not a tag, so there is nothing to compare against"
        if not me.socket_source:
            return f"the Docker socket isn't mounted at {DOCKER_SOCKET}"
        return ""

    async def status(self) -> SelfStatus:
        """UAM's container, running image, and the result of the last check."""
        state = await self._load()
        if state.get("updating_since"):
            await self.finish_pending()
            state = await self._load()
        me, reason = await self._locate()
        common: dict[str, Any] = {
            "checked_at": datetime.fromisoformat(state["checked_at"]) if state.get("checked_at") else None,
            "error": str(state.get("error") or ""),
            "updating": self._updating(state),
            "last_result": state.get("last_result"),
        }
        if me is None:
            return SelfStatus(supported=False, reason=reason, **common)
        latest = state.get("latest_digest") if state.get("image") == me.image else None
        return SelfStatus(
            supported=not self._blocker(me),
            reason=self._blocker(me),
            container_name=me.name,
            image=me.image,
            image_version=me.labels.get(LABEL_VERSION, ""),
            revision=me.labels.get(LABEL_REVISION, ""),
            created=me.labels.get(LABEL_CREATED, ""),
            current_digest=me.digest,
            latest_digest=latest,
            **common,
        )

    # ----------------------------------------------------------------- checking
    async def check(self) -> SelfStatus:
        """Ask the registry for the newest digest of UAM's image tag. Errors are stored, not raised."""
        me, _ = await self._locate()
        state = await self._load()
        if me is None or self._blocker(me):
            return await self.status()
        state["image"] = me.image
        state["checked_at"] = self._clock().isoformat()
        try:
            auth = await self._vault.registry_auth_for(me.image)
            state["latest_digest"] = await self._registry.get_digest(me.image, auth)
            state["error"] = ""
        except Exception as exc:  # noqa: BLE001 - shown on the page
            state["error"] = redact(f"update check failed: {exc}")
            log.warning("%s", state["error"])
        await self._save(state)
        status = await self.status()
        if status.update_available and state.get("notified_digest") != status.latest_digest:
            state["notified_digest"] = status.latest_digest
            await self._save(state)
            await self._events.record(
                None, "self_update_available", f"a new UAM image is available ({me.image})"
            )
            if not self._auto_update:
                await self._notify("UAM update available", f"A new image for {me.image} is ready to install.")
        if (
            status.update_available
            and self._auto_update
            and not status.updating
            and state.get("failed_digest") != status.latest_digest  # don't retry a rolled-back image
        ):
            await self.start_update()
            status = await self.status()
        return status

    async def scheduled_check(self) -> None:
        """The scheduler's periodic check; never raises."""
        try:
            await self.check()
        except Exception:
            log.exception("self-update check failed")

    # ----------------------------------------------------------------- updating
    async def start_update(self) -> None:
        """Validate, then pull and hand over to the helper in the background.

        ValidationError when UAM can't update itself here; ConflictError while an update runs.
        """
        me, reason = await self._locate()
        if me is None:
            raise ValidationError(reason)
        blocker = self._blocker(me)
        if blocker:
            raise ValidationError(blocker)
        state = await self._load()
        if self._updating(state):
            raise ConflictError("an update of UAM is already running")
        state["updating_since"] = self._clock().isoformat()
        await self._save(state)
        task = asyncio.create_task(self.run_update(me))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait(self) -> None:
        """Wait for background updates started by `start_update` (tests and shutdown)."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def run_update(self, me: _Self) -> None:
        """Pull the new image and start the helper. On failure nothing has changed; the error is kept."""
        try:
            auth = await self._vault.registry_auth_for(me.image)
            digest = await self._docker.pull(me.image, auth)
            if digest == me.digest:
                await self._finish({"ok": True, "message": "UAM is already up to date"}, notify=False)
                return
            state = await self._load()
            state["attempt_digest"] = digest
            await self._save(state)
            await self._events.record(
                None, "self_update_started", f"updating UAM to {me.image} ({digest[:19]})"
            )
            await asyncio.to_thread(self._launch_helper, me)
        except Exception as exc:  # noqa: BLE001 - background task boundary
            message = redact(f"UAM update failed before anything changed: {exc}")
            log.error("%s", message)
            await self._finish({"ok": False, "message": message})

    def _launch_helper(self, me: _Self) -> None:
        name = me.name + HELPER_SUFFIX
        stale = self._engine.inspect_container(name)
        if stale is not None and ((stale.get("Config") or {}).get("Labels") or {}).get(LABEL_HELPER):
            self._engine.remove(str(stale["Id"]), force=True)
        cmd = ["python", "-m", "uam.self_update", "--target", me.id, "--image", me.image]
        binds = [f"{me.socket_source}:{DOCKER_SOCKET}"]
        if me.data_source:
            binds.append(f"{me.data_source}:{HELPER_DATA_DIR}")
            cmd += ["--result", f"{HELPER_DATA_DIR}/{RESULT_FILE}"]
        # Prefer the image UAM runs now: its helper code is the code that started this update.
        image = me.image_id if me.image_id and self._engine.inspect_image(me.image_id) else me.image
        config: dict[str, Any] = {
            "Image": image,
            "Cmd": cmd,
            "Labels": {LABEL_HELPER: "helper"},
            "Healthcheck": {"Test": ["NONE"]},
            "HostConfig": {"Binds": binds, "AutoRemove": True, "NetworkMode": "none"},
        }
        helper_id = self._engine.create(config, name)
        self._engine.start(helper_id)

    async def _finish(self, result: dict[str, Any], notify: bool = True) -> None:
        state = await self._load()
        state.pop("updating_since", None)
        attempt = state.pop("attempt_digest", None)
        if not result.get("ok") and attempt:
            state["failed_digest"] = attempt
        result = {**result, "at": self._clock().isoformat()}
        state["last_result"] = result
        await self._save(state)
        ok = bool(result.get("ok"))
        await self._events.record(
            None,
            "self_update" if ok else "self_update_failed",
            str(result.get("message", "")),
            "info" if ok else "error",
        )
        if notify:
            title = "UAM updated" if ok else "UAM update failed"
            await self._notify(title, str(result.get("message", "")), 0 if ok else 1)

    async def finish_pending(self) -> None:
        """Report what the helper did, once it has written its result, and clear the 'updating' flag.

        Runs at startup and every minute: the helper writes its result only after the new container
        passed its health check, which is after the new UAM has already started.
        """
        path = self._data_dir / RESULT_FILE
        try:
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except FileNotFoundError:
            raw = None
        except OSError:
            log.exception("could not read %s", path)
            raw = None
        if raw is None:
            state = await self._load()
            if state.get("updating_since") and not self._updating(state):
                await self._finish(
                    {"ok": False, "message": "the UAM update didn't report back"}, notify=False
                )
            return
        try:
            result = json.loads(raw)
        except ValueError:
            result = {"ok": False, "message": "the UAM update left an unreadable result"}
        if not isinstance(result, dict):
            result = {"ok": False, "message": "the UAM update left an unreadable result"}
        try:
            await asyncio.to_thread(path.unlink)
        except OSError:
            log.exception("could not remove %s", path)
            return  # report it once the file can be removed, not every minute
        message = str(result.get("message") or "")
        if result.get("ok") and message == "updated":
            me, _ = await self._locate()
            revision = me.labels.get(LABEL_REVISION, "") if me else ""
            running = f"revision {revision[:7]}" if revision else f"version {package_version()}"
            result["message"] = f"UAM was updated and is now running {running}"
        elif result.get("rolled_back"):
            result["message"] = f"UAM update was rolled back: {message}"
        await self._finish(result)

    async def _notify(self, title: str, message: str, priority: int = 0) -> None:
        if self._notifier is not None:
            await self._notifier.notify("update", title, message, priority)
