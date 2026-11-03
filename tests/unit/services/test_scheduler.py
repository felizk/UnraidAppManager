from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from tests.conftest import FrozenClock
from tests.fakes import FakeBundle, make_app, make_source
from uam.domain import App, AppState, UpdateCheckResult
from uam.services.scheduler import UpdateScheduler
from uam.services.update_checker import UpdateChecker


class StubChecker:
    def __init__(self) -> None:
        self.checked: list[str] = []
        self.fail_on: set[str] = set()
        self.active = 0
        self.peak = 0

    async def check(self, app: App) -> UpdateCheckResult:
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        self.checked.append(app.name)
        if app.name in self.fail_on:
            raise RuntimeError("boom")
        raise NotImplementedError  # result unused by the scheduler


class Syncer:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def sync_state(self) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("x")


async def _apps(fakes: FakeBundle, names: list[str], **kw: Any) -> None:
    for n in names:
        await fakes.apps.add(make_app(name=n, state=AppState.RUNNING, **kw))


def build(fakes: FakeBundle, clock: FrozenClock, checker: Any, syncer: Syncer) -> UpdateScheduler:
    return UpdateScheduler(checker, fakes.apps, syncer, clock)


async def test_tick_checks_only_due_apps(fakes: FakeBundle, frozen_now: FrozenClock) -> None:
    await _apps(fakes, ["new"])
    await _apps(fakes, ["due"], next_check_at=frozen_now.now - timedelta(minutes=1))
    await _apps(fakes, ["later"], next_check_at=frozen_now.now + timedelta(minutes=1))
    checker = StubChecker()
    await build(fakes, frozen_now, checker, Syncer()).tick()
    assert sorted(checker.checked) == ["due", "new"]


async def test_failure_does_not_stop_others(fakes: FakeBundle, frozen_now: FrozenClock) -> None:
    await _apps(fakes, ["a", "b", "c"])
    checker = StubChecker()
    checker.fail_on = {"a"}
    await build(fakes, frozen_now, checker, Syncer()).tick()
    assert sorted(checker.checked) == ["a", "b", "c"]


async def test_concurrency_limit(fakes: FakeBundle, frozen_now: FrozenClock) -> None:
    await _apps(fakes, [f"app{i}" for i in range(10)])
    checker = StubChecker()
    await build(fakes, frozen_now, checker, Syncer()).tick()
    assert len(checker.checked) == 10
    assert checker.peak == 4


async def test_real_checker_failure_isolated(fakes: FakeBundle, frozen_now: FrozenClock) -> None:
    src = await fakes.sources.add(make_source())
    for n in ("bad", "good"):
        await fakes.apps.add(
            make_app(name=n, source_id=src.id, template_path=f"{n}.xml", state=AppState.RUNNING)
        )
    fakes.github.put_file(
        "felizk", "notes", "good.xml", "<Container><Repository>img:1</Repository></Container>"
    )
    fakes.registry.set_digest("img:1", "sha256:1")

    class Updater:
        async def update(self, app_id: int, apply_template: bool, apply_image: bool) -> App:
            raise AssertionError

    checker = UpdateChecker(
        fakes.apps,
        fakes.sources,
        fakes.github,
        fakes.registry,
        fakes.vault,
        fakes.codec,
        fakes.settings,
        Updater(),
        fakes.events,
        frozen_now,
    )
    await build(fakes, frozen_now, checker, Syncer()).tick()
    good = await fakes.apps.get_by_name("good")
    bad = await fakes.apps.get_by_name("bad")
    assert good is not None and good.last_checked_at == frozen_now.now
    assert bad is not None and bad.last_error


async def test_sync_errors_swallowed(fakes: FakeBundle, frozen_now: FrozenClock) -> None:
    syncer = Syncer()
    syncer.fail = True
    await build(fakes, frozen_now, StubChecker(), syncer).sync()
    assert syncer.calls == 1


async def test_start_registers_jobs_and_shutdown(fakes: FakeBundle, frozen_now: FrozenClock) -> None:
    sched = build(fakes, frozen_now, StubChecker(), Syncer())
    sched.start()
    try:
        jobs = {j.id: j for j in sched._scheduler.get_jobs()}
        assert set(jobs) == {"tick", "sync_state"}
        assert jobs["tick"].trigger.interval == timedelta(seconds=60)
        assert jobs["sync_state"].trigger.interval == timedelta(minutes=1)
        assert jobs["tick"].max_instances == 1 and jobs["tick"].coalesce
    finally:
        sched.shutdown()
    sched.shutdown()


class SelfJobs:
    def __init__(self) -> None:
        self.checks = 0
        self.finishes = 0

    async def scheduled_check(self) -> None:
        self.checks += 1

    async def finish_pending(self) -> None:
        self.finishes += 1
        raise RuntimeError("unreadable")


async def test_self_update_jobs(fakes: FakeBundle, frozen_now: FrozenClock) -> None:
    jobs_impl = SelfJobs()
    sched = UpdateScheduler(StubChecker(), fakes.apps, Syncer(), frozen_now, self_update=jobs_impl)  # type: ignore[arg-type]
    sched.start()
    try:
        jobs = {j.id: j for j in sched._scheduler.get_jobs()}
        assert set(jobs) == {"tick", "sync_state", "self_check", "self_result"}
        assert jobs["self_check"].trigger.interval == timedelta(hours=6)
        assert jobs["self_result"].trigger.interval == timedelta(minutes=1)
        await jobs["self_result"].func()  # errors are logged, not raised
        assert jobs_impl.finishes == 1
    finally:
        sched.shutdown()
