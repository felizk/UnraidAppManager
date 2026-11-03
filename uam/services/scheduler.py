"""APScheduler wiring: periodic update checks and state sync."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Protocol

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from uam.contracts import AppRepository
from uam.domain import App
from uam.services.update_checker import UpdateChecker

log = logging.getLogger(__name__)

TICK_SECONDS = 60
SYNC_MINUTES = 1
CONCURRENCY = 4
SELF_CHECK_HOURS = 6


class SelfUpdateJobs(Protocol):
    """The slice of `SelfUpdater` that the scheduler runs."""

    async def scheduled_check(self) -> None: ...

    async def finish_pending(self) -> None: ...


class StateSyncer(Protocol):
    """The slice of the orchestrator that the scheduler needs."""

    async def sync_state(self) -> None: ...


class UpdateScheduler:
    """Runs `tick` and `sync_state` (which also spots crashed apps) every minute."""

    def __init__(
        self,
        checker: UpdateChecker,
        apps: AppRepository,
        syncer: StateSyncer,
        clock: Callable[[], datetime],
        concurrency: int = CONCURRENCY,
        self_update: SelfUpdateJobs | None = None,
    ) -> None:
        self._checker = checker
        self._apps = apps
        self._syncer = syncer
        self._clock = clock
        self._concurrency = concurrency
        self._self_update = self_update
        self._scheduler = AsyncIOScheduler()

    def start(self) -> None:
        """Register the jobs and start the scheduler (call from the FastAPI lifespan)."""
        self._scheduler.add_job(
            self.tick, "interval", seconds=TICK_SECONDS, id="tick", max_instances=1, coalesce=True
        )
        self._scheduler.add_job(
            self.sync, "interval", minutes=SYNC_MINUTES, id="sync_state", max_instances=1, coalesce=True
        )
        if self._self_update is not None:
            first = self._clock() + timedelta(minutes=2)
            self._scheduler.add_job(
                self._self_update.scheduled_check,
                "interval",
                hours=SELF_CHECK_HOURS,
                next_run_time=first,
                id="self_check",
                max_instances=1,
                coalesce=True,
            )
            self._scheduler.add_job(
                self._guard(self._self_update.finish_pending, "self-update result"),
                "interval",
                minutes=1,
                next_run_time=self._clock(),
                id="self_result",
                max_instances=1,
                coalesce=True,
            )
        self._scheduler.start()

    @staticmethod
    def _guard(job: Callable[[], Awaitable[None]], what: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            try:
                await job()
            except Exception:
                log.exception("%s failed", what)

        return run

    def shutdown(self) -> None:
        """Stop the scheduler without waiting for running jobs."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def tick(self) -> None:
        """Check every app that is due, at most `concurrency` at a time. One failure never stops the rest."""
        due = await self._apps.due_for_check(self._clock())
        sem = asyncio.Semaphore(self._concurrency)

        async def run(app: App) -> None:
            async with sem:
                try:
                    await self._checker.check(app)
                except Exception:
                    log.exception("update check failed for %s", app.name)

        await asyncio.gather(*(run(a) for a in due))

    async def sync(self) -> None:
        """Reconcile app state with Docker; errors are logged, not raised."""
        try:
            await self._syncer.sync_state()
        except Exception:
            log.exception("sync_state failed")
