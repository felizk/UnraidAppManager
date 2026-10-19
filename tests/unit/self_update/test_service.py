from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FrozenClock
from tests.fakes import FakeBundle
from uam.domain import ContainerInfo
from uam.errors import ConflictError, RemoteError, ValidationError
from uam.self_update.service import RESULT_FILE, STATE_KEY, SelfUpdater

OWN_ID = "c" * 64
IMAGE = "ghcr.io/felizk/unraid-app-manager:latest"
IMAGE_ID = "sha256:" + "1" * 64
OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64


class Engine:
    """Raw inspect JSON for UAM's own container, and a record of helper launches."""

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {
            OWN_ID: {
                "Id": OWN_ID,
                "Name": "/unraid-app-manager",
                "Image": IMAGE_ID,
                "Config": {"Image": IMAGE},
                "Mounts": [
                    {"Destination": "/var/run/docker.sock", "Source": "/var/run/docker.sock"},
                    {"Destination": "/data", "Source": "/mnt/user/appdata/unraid-app-manager"},
                ],
            }
        }
        self.images: dict[str, dict[str, Any]] = {IMAGE_ID: {"Id": IMAGE_ID}}
        self.created: list[tuple[dict[str, Any], str]] = []
        self.started: list[str] = []
        self.removed: list[str] = []

    def inspect_container(self, ref: str) -> dict[str, Any] | None:
        if ref in self.containers:
            return self.containers[ref]
        return next((c for c in self.containers.values() if c["Name"] == "/" + ref), None)

    def inspect_image(self, ref: str) -> dict[str, Any] | None:
        return self.images.get(ref)

    def create(self, config: dict[str, Any], name: str) -> str:
        self.created.append((config, name))
        return "helper1"

    def connect(self, network: str, container_id: str, endpoint: dict[str, Any]) -> None:
        raise AssertionError("not used")

    def start(self, container_id: str) -> None:
        self.started.append(container_id)

    def stop(self, container_id: str, timeout: int) -> None:
        raise AssertionError("not used")

    def remove(self, container_id: str, force: bool = False) -> None:
        self.removed.append(container_id)

    def rename(self, container_id: str, name: str) -> None:
        raise AssertionError("not used")


@pytest.fixture
def engine() -> Engine:
    return Engine()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path


def make(
    fakes: FakeBundle,
    engine: Engine,
    clock: FrozenClock,
    data_dir: Path,
    *,
    ref: str | None = OWN_ID,
    auto_update: bool = False,
) -> SelfUpdater:
    fakes.docker.seed_container(
        ContainerInfo(
            id=OWN_ID,
            name="unraid-app-manager",
            image=IMAGE,
            image_digest=OLD,
            state="running",
            health="healthy",
            labels={},
            published_ports=(),
            image_labels={"org.opencontainers.image.revision": "abcdef1234"},
        )
    )
    # The container mounts the data folder at the directory UAM was told to use.
    engine.containers[OWN_ID]["Mounts"][1]["Destination"] = str(data_dir)
    return SelfUpdater(
        engine,
        fakes.docker,
        fakes.registry,
        fakes.vault,
        fakes.kv,
        fakes.events,
        clock,
        container_ref=ref,
        data_dir=data_dir,
        notifier=fakes.notifier,
        auto_update=auto_update,
    )


async def test_status_outside_a_container(fakes, engine, frozen_now, data_dir) -> None:
    status = await make(fakes, engine, frozen_now, data_dir, ref=None).status()
    assert not status.supported and "isn't running in a Docker container" in status.reason
    status = await make(fakes, engine, frozen_now, data_dir, ref="f" * 64).status()
    assert not status.supported and "isn't visible" in status.reason


async def test_status_describes_the_running_container(fakes, engine, frozen_now, data_dir) -> None:
    status = await make(fakes, engine, frozen_now, data_dir).status()
    assert status.supported and status.container_name == "unraid-app-manager"
    assert (status.image, status.current_digest, status.revision) == (IMAGE, OLD, "abcdef1234")
    assert status.latest_digest is None and not status.update_available and status.checked_at is None


@pytest.mark.parametrize(
    ("image", "mounts", "reason"),
    [
        (IMAGE.replace(":latest", "@" + OLD), None, "pinned to a digest"),
        (IMAGE_ID, None, "image id"),
        (IMAGE, [], "Docker socket isn't mounted"),
    ],
)
async def test_unsupported_setups(fakes, engine, frozen_now, data_dir, image, mounts, reason) -> None:
    updater = make(fakes, engine, frozen_now, data_dir)
    engine.containers[OWN_ID]["Config"]["Image"] = image
    if mounts is not None:
        engine.containers[OWN_ID]["Mounts"] = mounts
    status = await updater.check()
    assert not status.supported and reason in status.reason
    assert fakes.registry.calls == []
    with pytest.raises(ValidationError, match=reason):
        await updater.start_update()


async def test_check_finds_an_update_and_notifies_once(fakes, engine, frozen_now, data_dir) -> None:
    updater = make(fakes, engine, frozen_now, data_dir)
    fakes.registry.set_digest(IMAGE, NEW)
    status = await updater.check()
    assert status.update_available and status.latest_digest == NEW
    assert status.checked_at == frozen_now()
    assert fakes.notifier.sent == [
        ("update", "UAM update available", f"A new image for {IMAGE} is ready to install.", 0)
    ]
    assert fakes.events.kinds() == ["self_update_available"]
    await updater.check()
    assert len(fakes.notifier.sent) == 1 and len(fakes.events.events) == 1


async def test_check_up_to_date(fakes, engine, frozen_now, data_dir) -> None:
    updater = make(fakes, engine, frozen_now, data_dir)
    fakes.registry.set_digest(IMAGE, OLD)
    status = await updater.check()
    assert not status.update_available and status.latest_digest == OLD and fakes.notifier.sent == []


async def test_check_failure_is_stored(fakes, engine, frozen_now, data_dir, monkeypatch) -> None:
    updater = make(fakes, engine, frozen_now, data_dir)

    async def boom(image_ref: str, auth: object) -> str:
        raise RemoteError(500, "token=ghp_secret123")

    monkeypatch.setattr(fakes.registry, "get_digest", boom)
    status = await updater.check()
    assert status.error.startswith("update check failed") and "ghp_secret123" not in status.error


async def test_update_pulls_and_launches_the_helper(fakes, engine, frozen_now, data_dir) -> None:
    updater = make(fakes, engine, frozen_now, data_dir)
    fakes.docker.set_image(IMAGE, NEW)
    await updater.start_update()
    assert (await updater.status()).updating
    with pytest.raises(ConflictError):
        await updater.start_update()
    await updater.wait()
    assert fakes.docker.pulls[0][0] == IMAGE
    config, name = engine.created[0]
    assert name == "unraid-app-manager-self-update" and engine.started == ["helper1"]
    assert config["Image"] == IMAGE_ID
    assert config["Cmd"] == [
        "python",
        "-m",
        "uam.self_update",
        "--target",
        OWN_ID,
        "--image",
        IMAGE,
        "--result",
        f"/uam-data/{RESULT_FILE}",
    ]
    assert config["HostConfig"] == {
        "Binds": [
            "/var/run/docker.sock:/var/run/docker.sock",
            "/mnt/user/appdata/unraid-app-manager:/uam-data",
        ],
        "AutoRemove": True,
        "NetworkMode": "none",
    }
    assert fakes.events.kinds() == ["self_update_started"]
    assert (await updater.status()).updating  # until the helper reports back


async def test_helper_falls_back_to_the_tag_and_replaces_a_stale_helper(fakes, engine, frozen_now, data_dir):
    updater = make(fakes, engine, frozen_now, data_dir)
    del engine.images[IMAGE_ID]
    engine.containers["old-helper"] = {
        "Id": "old-helper",
        "Name": "/unraid-app-manager-self-update",
        "Config": {"Labels": {"uam.self_update": "helper"}},
    }
    fakes.docker.set_image(IMAGE, NEW)
    await updater.start_update()
    await updater.wait()
    assert engine.removed == ["old-helper"]
    assert engine.created[0][0]["Image"] == IMAGE


async def test_update_when_already_current(fakes, engine, frozen_now, data_dir) -> None:
    updater = make(fakes, engine, frozen_now, data_dir)
    fakes.docker.set_image(IMAGE, OLD)
    await updater.start_update()
    await updater.wait()
    status = await updater.status()
    assert not status.updating and engine.created == []
    assert status.last_result and status.last_result["message"] == "UAM is already up to date"
    assert fakes.notifier.sent == []


async def test_pull_failure_changes_nothing(fakes, engine, frozen_now, data_dir) -> None:
    updater = make(fakes, engine, frozen_now, data_dir)
    fakes.docker.fail_next("pull", RemoteError(401, "denied"))
    await updater.start_update()
    await updater.wait()
    status = await updater.status()
    assert not status.updating and engine.created == []
    assert status.last_result and not status.last_result["ok"]
    assert "before anything changed" in status.last_result["message"]
    assert fakes.notifier.sent[0][1] == "UAM update failed"


async def _launch(updater: SelfUpdater, fakes: FakeBundle) -> None:
    fakes.docker.set_image(IMAGE, NEW)
    await updater.start_update()
    await updater.wait()


async def test_finish_pending_reports_success(fakes, engine, frozen_now, data_dir) -> None:
    updater = make(fakes, engine, frozen_now, data_dir)
    await _launch(updater, fakes)
    (data_dir / RESULT_FILE).write_text(json.dumps({"ok": True, "message": "updated", "rolled_back": False}))
    status = await updater.status()  # the page notices the result straight away
    assert not status.updating
    assert (
        status.last_result
        and status.last_result["message"] == "UAM was updated and is now running revision abcdef1"
    )
    assert not (data_dir / RESULT_FILE).exists()
    assert fakes.notifier.sent[-1][:2] == ("update", "UAM updated")
    assert fakes.events.kinds()[-1] == "self_update"


async def test_finish_pending_reports_a_rollback(fakes, engine, frozen_now, data_dir) -> None:
    updater = make(fakes, engine, frozen_now, data_dir)
    await _launch(updater, fakes)
    result = {"ok": False, "message": "health check failed: boom", "rolled_back": True}
    (data_dir / RESULT_FILE).write_text(json.dumps(result))
    await updater.finish_pending()
    status = await updater.status()
    assert (
        status.last_result
        and status.last_result["message"] == "UAM update was rolled back: health check failed: boom"
    )
    assert fakes.notifier.sent[-1] == ("update", "UAM update failed", status.last_result["message"], 1)
    assert json.loads(await fakes.kv.get(STATE_KEY) or "{}")["failed_digest"] == NEW


async def test_stale_update_flag_is_cleared(fakes, engine, frozen_now, data_dir) -> None:
    updater = make(fakes, engine, frozen_now, data_dir)
    await _launch(updater, fakes)
    await updater.finish_pending()
    assert (await updater.status()).updating
    frozen_now.advance(timedelta(minutes=20))
    status = await updater.status()
    assert not status.updating
    assert status.last_result and status.last_result["message"] == "the UAM update didn't report back"


async def test_unreadable_result(fakes, engine, frozen_now, data_dir) -> None:
    updater = make(fakes, engine, frozen_now, data_dir)
    (data_dir / RESULT_FILE).write_text("{not json")
    await updater.finish_pending()
    status = await updater.status()
    assert status.last_result and "unreadable" in status.last_result["message"]


async def test_auto_update_starts_but_skips_a_rolled_back_image(fakes, engine, frozen_now, data_dir) -> None:
    updater = make(fakes, engine, frozen_now, data_dir, auto_update=True)
    fakes.registry.set_digest(IMAGE, NEW)
    fakes.docker.set_image(IMAGE, NEW)
    await updater.check()
    await updater.wait()
    assert len(engine.created) == 1
    assert all(n[1] != "UAM update available" for n in fakes.notifier.sent)
    (data_dir / RESULT_FILE).write_text(json.dumps({"ok": False, "message": "x", "rolled_back": True}))
    await updater.finish_pending()
    await updater.scheduled_check()
    await updater.wait()
    assert len(engine.created) == 1  # the same image isn't tried again
