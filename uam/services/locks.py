"""Per-app operation locks. A second operation on a busy app is rejected, not queued."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from uam.errors import ConflictError


class AppLocks:
    """One `asyncio.Lock` per app id with fail-fast acquisition."""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    def locked(self, app_id: int) -> bool:
        """True while an operation holds the lock for `app_id`."""
        lock = self._locks.get(app_id)
        return lock is not None and lock.locked()

    @asynccontextmanager
    async def hold(self, app_id: int) -> AsyncIterator[None]:
        """Hold the lock for `app_id`; raise `ConflictError` if it is already held."""
        lock = self._locks.setdefault(app_id, asyncio.Lock())
        if lock.locked():
            raise ConflictError("operation in progress")
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()
            if not lock.locked():
                self._locks.pop(app_id, None)
