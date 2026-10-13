"""In-memory implementations of the persistence Protocols."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from uam.db.repositories import CHECKABLE_STATES
from uam.domain import App, Source
from uam.errors import ConflictError, NotFound
from uam.settings import Settings


class InMemoryAppRepository:
    def __init__(self) -> None:
        self.apps: dict[int, App] = {}
        self._next = 1

    async def get(self, app_id: int) -> App:
        if app_id not in self.apps:
            raise NotFound(f"app {app_id}")
        return copy.deepcopy(self.apps[app_id])

    async def get_by_name(self, name: str) -> App | None:
        for app in self.apps.values():
            if app.name == name:
                return copy.deepcopy(app)
        return None

    async def due_for_check(self, now: datetime) -> list[App]:
        due = [
            a
            for a in self.apps.values()
            if a.state in CHECKABLE_STATES and (a.next_check_at is None or a.next_check_at <= now)
        ]
        due.sort(key=lambda a: (a.next_check_at is not None, a.next_check_at or now, a.id))
        return [copy.deepcopy(a) for a in due]

    async def add(self, app: App) -> App:
        if any(a.name == app.name for a in self.apps.values()):
            raise ConflictError(f"app name {app.name!r} already exists")
        stored = copy.deepcopy(app)
        stored.id = self._next
        self._next += 1
        self.apps[stored.id] = stored
        return copy.deepcopy(stored)

    async def save(self, app: App) -> None:
        if app.id not in self.apps:
            raise NotFound(f"app {app.id}")
        if any(a.name == app.name and a.id != app.id for a in self.apps.values()):
            raise ConflictError(f"app name {app.name!r} already exists")
        self.apps[app.id] = copy.deepcopy(app)

    async def delete(self, app_id: int) -> None:
        self.apps.pop(app_id, None)

    async def list(self) -> list[App]:
        return [copy.deepcopy(a) for a in sorted(self.apps.values(), key=lambda a: a.name)]


class InMemorySourceRepository:
    def __init__(self, apps: InMemoryAppRepository | None = None) -> None:
        self.sources: dict[int, Source] = {}
        self._apps = apps
        self._next = 1

    async def get(self, source_id: int) -> Source:
        if source_id not in self.sources:
            raise NotFound(f"source {source_id}")
        return self.sources[source_id]

    async def add(self, source: Source) -> Source:
        stored = replace(source, id=self._next)
        self._next += 1
        self.sources[stored.id] = stored
        return stored

    async def update(self, source: Source) -> None:
        if source.id not in self.sources:
            raise NotFound(f"source {source.id}")
        old = self.sources[source.id]
        self.sources[source.id] = replace(
            old, ref=source.ref, template_path=source.template_path, credential_id=source.credential_id
        )

    async def delete(self, source_id: int) -> None:
        if self._apps and any(a.source_id == source_id for a in self._apps.apps.values()):
            raise ConflictError("source still has apps")
        self.sources.pop(source_id, None)

    async def list(self) -> list[Source]:
        return sorted(self.sources.values(), key=lambda s: (s.owner, s.repo))


class InMemorySettingsStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    async def get(self) -> Settings:
        return copy.deepcopy(self.settings)

    async def save(self, settings: Settings) -> None:
        self.settings = copy.deepcopy(settings)


class InMemoryKeyValueStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


class InMemoryHttpCache:
    def __init__(self) -> None:
        self.data: dict[str, tuple[str, str]] = {}

    async def get(self, key: str) -> tuple[str, str] | None:
        return self.data.get(key)

    async def put(self, key: str, etag: str, body: str) -> None:
        self.data[key] = (etag, body)


class InMemoryEventLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def record(self, app_id: int | None, kind: str, message: str, level: str = "info") -> None:
        self.events.append(
            {
                "id": len(self.events) + 1,
                "ts": datetime.now(UTC),
                "app_id": app_id,
                "kind": kind,
                "level": level,
                "message": message,
            }
        )

    async def recent(self, app_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        events = [e for e in reversed(self.events) if app_id is None or e["app_id"] == app_id]
        return events[:limit]

    def kinds(self, app_id: int | None = None) -> list[str]:
        """Event kinds in insertion order (test helper)."""
        return [e["kind"] for e in self.events if app_id is None or e["app_id"] == app_id]
