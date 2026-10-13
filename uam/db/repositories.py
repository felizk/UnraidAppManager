"""SQL implementations of the repository Protocols in `uam.contracts`."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from uam.contracts import FieldCipher
from uam.db.models import AppRow, EventRow, HttpCacheRow, KeyValueRow, SettingsRow, SourceRow
from uam.domain import App, AppState, ProxySettings, Source
from uam.errors import ConflictError, NotFound
from uam.settings import Settings

SessionFactory = async_sessionmaker[AsyncSession]

# States in which an app has a container worth checking for updates.
CHECKABLE_STATES = (AppState.RUNNING, AppState.STOPPED, AppState.FAILED_UPDATE)


def _proxy_from_json(data: dict[str, Any]) -> ProxySettings:
    return ProxySettings(**data) if data else ProxySettings()


class SqlAppRepository:
    """Apps table. Overrides are encrypted with `cipher` because they can hold masked template values."""

    def __init__(self, session_factory: SessionFactory, cipher: FieldCipher) -> None:
        self._sf = session_factory
        self._cipher = cipher

    def _to_domain(self, row: AppRow) -> App:
        overrides: dict[str, str] = json.loads(self._cipher.decrypt(row.overrides_ciphertext))
        return App(
            id=row.id,
            source_id=row.source_id,
            template_path=row.template_path,
            name=row.name,
            overrides=overrides,
            image_tag_override=row.image_tag_override,
            auto_ports=row.auto_ports,
            auto_update_image=row.auto_update_image,
            auto_update_template=row.auto_update_template,
            proxy=_proxy_from_json(row.proxy),
            state=AppState(row.state),
            installed_template_commit=row.installed_template_commit,
            installed_image_digest=row.installed_image_digest,
            container_id=row.container_id,
            available_template_commit=row.available_template_commit,
            available_image_digest=row.available_image_digest,
            needs_attention=row.needs_attention,
            last_change_at=row.last_change_at,
            last_checked_at=row.last_checked_at,
            next_check_at=row.next_check_at,
            last_error=row.last_error,
            labels_pending=row.labels_pending,
        )

    def _apply(self, row: AppRow, app: App) -> None:
        row.source_id = app.source_id
        row.template_path = app.template_path
        row.name = app.name
        row.overrides_ciphertext = self._cipher.encrypt(json.dumps(app.overrides, sort_keys=True).encode())
        row.image_tag_override = app.image_tag_override
        row.auto_ports = app.auto_ports
        row.auto_update_image = app.auto_update_image
        row.auto_update_template = app.auto_update_template
        row.proxy = asdict(app.proxy)
        row.state = app.state.value
        row.installed_template_commit = app.installed_template_commit
        row.installed_image_digest = app.installed_image_digest
        row.container_id = app.container_id
        row.available_template_commit = app.available_template_commit
        row.available_image_digest = app.available_image_digest
        row.needs_attention = app.needs_attention
        row.last_change_at = app.last_change_at
        row.last_checked_at = app.last_checked_at
        row.next_check_at = app.next_check_at
        row.last_error = app.last_error
        row.labels_pending = app.labels_pending

    async def get(self, app_id: int) -> App:
        """Return the app or raise NotFound."""
        async with self._sf() as s:
            row = await s.get(AppRow, app_id)
            if row is None:
                raise NotFound(f"app {app_id}")
            return self._to_domain(row)

    async def get_by_name(self, name: str) -> App | None:
        """Return the app with this container name, if any."""
        async with self._sf() as s:
            row = (await s.execute(select(AppRow).where(AppRow.name == name))).scalar_one_or_none()
            return self._to_domain(row) if row else None

    async def due_for_check(self, now: datetime) -> list[App]:
        """Installed apps whose next check is unset or due."""
        async with self._sf() as s:
            stmt = (
                select(AppRow)
                .where(AppRow.state.in_([st.value for st in CHECKABLE_STATES]))
                .where(or_(AppRow.next_check_at.is_(None), AppRow.next_check_at <= now))
                .order_by(AppRow.next_check_at.is_not(None), AppRow.next_check_at, AppRow.id)
            )
            return [self._to_domain(r) for r in (await s.execute(stmt)).scalars()]

    async def add(self, app: App) -> App:
        """Insert a new app; raises ConflictError if the name is taken."""
        async with self._sf() as s:
            row = AppRow()
            self._apply(row, app)
            s.add(row)
            try:
                await s.commit()
            except IntegrityError as e:
                raise ConflictError(f"app name {app.name!r} already exists or source is missing") from e
            return self._to_domain(row)

    async def save(self, app: App) -> None:
        """Persist every field of an existing app."""
        async with self._sf() as s:
            row = await s.get(AppRow, app.id)
            if row is None:
                raise NotFound(f"app {app.id}")
            self._apply(row, app)
            try:
                await s.commit()
            except IntegrityError as e:
                raise ConflictError(f"app name {app.name!r} already exists") from e

    async def delete(self, app_id: int) -> None:
        """Delete the app (its port allocations cascade)."""
        async with self._sf() as s:
            await s.execute(delete(AppRow).where(AppRow.id == app_id))
            await s.commit()

    async def list(self) -> list[App]:
        """All apps ordered by name."""
        async with self._sf() as s:
            rows = (await s.execute(select(AppRow).order_by(AppRow.name))).scalars()
            return [self._to_domain(r) for r in rows]


class SqlSourceRepository:
    """Sources table."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    @staticmethod
    def _to_domain(row: SourceRow) -> Source:
        return Source(
            id=row.id,
            owner=row.owner,
            repo=row.repo,
            ref=row.ref,
            template_path=row.template_path,
            credential_id=row.credential_id,
        )

    async def get(self, source_id: int) -> Source:
        """Return the source or raise NotFound."""
        async with self._sf() as s:
            row = await s.get(SourceRow, source_id)
            if row is None:
                raise NotFound(f"source {source_id}")
            return self._to_domain(row)

    async def add(self, source: Source) -> Source:
        """Insert; `source.id` is ignored."""
        async with self._sf() as s:
            row = SourceRow(
                owner=source.owner,
                repo=source.repo,
                ref=source.ref,
                template_path=source.template_path,
                credential_id=source.credential_id,
            )
            s.add(row)
            try:
                await s.commit()
            except IntegrityError as e:
                raise ConflictError("unknown credential") from e
            return self._to_domain(row)

    async def update(self, source: Source) -> None:
        """Replace ref, template path and credential of an existing source."""
        async with self._sf() as s:
            row = await s.get(SourceRow, source.id)
            if row is None:
                raise NotFound(f"source {source.id}")
            row.ref, row.template_path, row.credential_id = (
                source.ref,
                source.template_path,
                source.credential_id,
            )
            try:
                await s.commit()
            except IntegrityError as e:
                raise ConflictError("unknown credential") from e

    async def delete(self, source_id: int) -> None:
        """Delete the source; ConflictError while apps still use it."""
        async with self._sf() as s:
            try:
                await s.execute(delete(SourceRow).where(SourceRow.id == source_id))
                await s.commit()
            except IntegrityError as e:
                raise ConflictError("source still has apps") from e

    async def list(self) -> list[Source]:
        """All sources ordered by owner/repo."""
        async with self._sf() as s:
            rows = (await s.execute(select(SourceRow).order_by(SourceRow.owner, SourceRow.repo))).scalars()
            return [self._to_domain(r) for r in rows]


class SqlSettingsStore:
    """Settings as a single JSON row (id=1)."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    async def get(self) -> Settings:
        """Stored settings, or defaults when none were saved."""
        async with self._sf() as s:
            row = await s.get(SettingsRow, 1)
            return Settings.from_dict(row.data) if row else Settings()

    async def save(self, settings: Settings) -> None:
        """Replace the stored settings."""
        async with self._sf() as s:
            row = await s.get(SettingsRow, 1)
            if row is None:
                s.add(SettingsRow(id=1, data=settings.to_dict()))
            else:
                row.data = settings.to_dict()
            await s.commit()


class SqlKeyValueStore:
    """Internal key/value pairs."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    async def get(self, key: str) -> str | None:
        """Value for `key`, or None."""
        async with self._sf() as s:
            row = await s.get(KeyValueRow, key)
            return row.value if row else None

    async def set(self, key: str, value: str) -> None:
        """Insert or replace."""
        async with self._sf() as s:
            await s.merge(KeyValueRow(key=key, value=value))
            await s.commit()

    async def delete(self, key: str) -> None:
        """Remove `key` if present."""
        async with self._sf() as s:
            await s.execute(delete(KeyValueRow).where(KeyValueRow.key == key))
            await s.commit()


class SqlHttpCache:
    """ETag cache rows."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    async def get(self, key: str) -> tuple[str, str] | None:
        """(etag, body) for `key`, or None."""
        async with self._sf() as s:
            row = await s.get(HttpCacheRow, key)
            return (row.etag, row.body) if row else None

    async def put(self, key: str, etag: str, body: str) -> None:
        """Insert or replace."""
        async with self._sf() as s:
            row = await s.get(HttpCacheRow, key)
            if row is None:
                s.add(HttpCacheRow(key=key, etag=etag, body=body))
            else:
                row.etag, row.body = etag, body
            await s.commit()


class SqlEventLog:
    """Audit events."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    async def record(self, app_id: int | None, kind: str, message: str, level: str = "info") -> None:
        """Append an event."""
        async with self._sf() as s:
            s.add(EventRow(app_id=app_id, kind=kind, message=message, level=level))
            await s.commit()

    async def recent(self, app_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Newest events first, optionally for a single app."""
        async with self._sf() as s:
            stmt = select(EventRow).order_by(EventRow.id.desc()).limit(limit)
            if app_id is not None:
                stmt = stmt.where(EventRow.app_id == app_id)
            return [
                {
                    "id": r.id,
                    "ts": r.ts,
                    "app_id": r.app_id,
                    "kind": r.kind,
                    "level": r.level,
                    "message": r.message,
                }
                for r in (await s.execute(stmt)).scalars()
            ]
