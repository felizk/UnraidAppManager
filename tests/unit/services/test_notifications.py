"""Orchestrator notifications: app updates and crashes."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.conftest import FrozenClock, load_template
from tests.fakes import FakeBundle, make_app, make_source
from uam.domain import AppState
from uam.errors import DeployError
from uam.services.orchestrator import Orchestrator

IMAGE = "ghcr.io/felizk/notes:latest"
OLD, NEW = "sha256:" + "a" * 64, "sha256:" + "b" * 64


class Env:
    def __init__(self, fakes: FakeBundle, clock: FrozenClock) -> None:
        self.f = fakes
        self.clock = clock
        self.orch = Orchestrator(
            fakes.apps,
            fakes.sources,
            fakes.github,
            fakes.codec,
            fakes.spec_resolver,
            fakes.docker,
            fakes.proxy,
            fakes.ports,
            fakes.vault,
            fakes.settings,
            fakes.events,
            clock,
            notifier=fakes.notifier,
        )

    async def installed(self) -> tuple[int, str]:
        app = await self.f.apps.add(make_app(source_id=1))
        await self.orch.install(app.id)
        stored = await self.f.apps.get(app.id)
        self.f.notifier.sent.clear()
        return app.id, stored.container_id or ""

    def set_container(self, cid: str, **changes: object) -> None:
        self.f.docker.containers[cid] = replace(self.f.docker.containers[cid], **changes)  # type: ignore[arg-type]

    def sent(self) -> list[tuple[str, str]]:
        return [(topic, title) for topic, title, _, _ in self.f.notifier.sent]


@pytest.fixture
async def env(fakes: FakeBundle, frozen_now: FrozenClock) -> Env:
    await fakes.sources.add(make_source())
    head = fakes.github.put_file("felizk", "notes", "unraid/notes.xml", load_template("simple-web.xml"))
    fakes.github.put_file(
        "felizk", "notes", "unraid/notes.xml", load_template("simple-web.xml"), ref=head.commit_sha
    )
    fakes.docker.set_image(IMAGE, OLD)
    return Env(fakes, frozen_now)


# ------------------------------------------------------------------ updates


async def test_update_sends_notification_with_new_digest(env: Env) -> None:
    app_id, _ = await env.installed()
    env.f.docker.set_image(IMAGE, NEW)
    await env.orch.update(app_id, apply_template=False, apply_image=True)
    ((topic, title, message, _),) = env.f.notifier.sent
    assert (topic, title) == ("update", "notes updated")
    assert "a" * 12 + " -> " + "b" * 12 in message


async def test_failed_update_notifies_rollback(env: Env) -> None:
    app_id, _ = await env.installed()
    env.f.docker.set_image(IMAGE, NEW)
    env.f.docker.fail_next("health")
    with pytest.raises(DeployError):
        await env.orch.update(app_id, apply_template=False, apply_image=True)
    ((topic, title, message, _),) = env.f.notifier.sent
    assert (topic, title) == ("update", "notes update failed")
    assert "previous version was restored" in message


async def test_install_and_reconfigure_do_not_notify(env: Env) -> None:
    app_id, _ = await env.installed()
    await env.orch.reconfigure(app_id, overrides={"TZ": "UTC"})
    assert env.f.notifier.sent == []


# ------------------------------------------------------------------ crashes


async def test_crashed_container_notifies_with_logs(env: Env) -> None:
    app_id, cid = await env.installed()
    env.f.docker.log_text[cid] = "boom: out of cheese"
    env.set_container(cid, state="exited", exit_code=1)
    await env.orch.sync_state()
    assert (await env.f.apps.get(app_id)).state == AppState.STOPPED
    ((topic, title, message, _),) = env.f.notifier.sent
    assert (topic, title) == ("crash", "notes crashed")
    assert "exit code 1" in message and "out of cheese" in message
    assert "sync.crashed" in env.f.events.kinds(app_id)


async def test_clean_stop_outside_uam_is_not_a_crash(env: Env) -> None:
    app_id, cid = await env.installed()
    env.set_container(cid, state="exited", exit_code=0)
    await env.orch.sync_state()
    assert env.f.notifier.sent == []
    assert "sync.stopped" in env.f.events.kinds(app_id)


async def test_stop_through_uam_is_not_a_crash(env: Env) -> None:
    app_id, _ = await env.installed()
    await env.orch.stop(app_id)
    await env.orch.sync_state()
    assert env.f.notifier.sent == []


async def test_restart_by_docker_counts_as_crash(env: Env) -> None:
    _, cid = await env.installed()
    await env.orch.sync_state()
    assert env.f.notifier.sent == []
    env.set_container(cid, restart_count=1)
    await env.orch.sync_state()
    assert env.sent() == [("crash", "notes crashed")]
    assert "restarted by Docker" in env.f.notifier.sent[0][2]


async def test_restarting_container_is_a_crash(env: Env) -> None:
    _, cid = await env.installed()
    env.set_container(cid, state="restarting", exit_code=2)
    await env.orch.sync_state()
    assert env.sent() == [("crash", "notes crashed")]
    assert "keeps crashing" in env.f.notifier.sent[0][2]


async def test_crash_notifications_are_throttled(env: Env) -> None:
    _, cid = await env.installed()
    await env.orch.sync_state()
    for count in (1, 2):
        env.set_container(cid, restart_count=count)
        await env.orch.sync_state()
    assert len(env.f.notifier.sent) == 1
    env.clock.advance(timedelta(hours=1, minutes=1))
    env.set_container(cid, restart_count=3)
    await env.orch.sync_state()
    assert len(env.f.notifier.sent) == 2


async def test_new_container_resets_restart_baseline(env: Env) -> None:
    app_id, cid = await env.installed()
    env.set_container(cid, restart_count=4)
    await env.orch.sync_state()
    env.f.docker.set_image(IMAGE, NEW)
    await env.orch.update(app_id, apply_template=False, apply_image=True)
    env.f.notifier.sent.clear()
    await env.orch.sync_state()
    assert env.f.notifier.sent == []


async def test_missing_container_notifies(env: Env) -> None:
    await env.installed()
    env.f.docker.containers.clear()
    await env.orch.sync_state()
    assert env.sent() == [("crash", "notes container is missing")]


async def test_no_notifier_is_fine(fakes: FakeBundle, frozen_now: FrozenClock, env: Env) -> None:
    env.orch._notifier = None
    _, cid = await env.installed()
    env.set_container(cid, state="exited", exit_code=1)
    await env.orch.sync_state()
