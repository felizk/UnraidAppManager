"""Database-backed host port allocator."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from uam.contracts import DockerEngine, SettingsStore
from uam.db.models import PortAllocationRow
from uam.domain import PortAllocation, Protocol_
from uam.errors import ConflictError, PortRangeExhausted

MAX_ATTEMPTS = 3
Key = tuple[int, Protocol_]


class HostListenerProbeLike(Protocol):
    """Anything that can report host listeners."""

    async def listening(self) -> set[Key]: ...


def _proto(row: PortAllocationRow) -> Protocol_:
    return "udp" if row.protocol == "udp" else "tcp"


def _to_alloc(row: PortAllocationRow) -> PortAllocation:
    return PortAllocation(row.app_id, row.container_port, _proto(row), row.host_port)


class DbPortAllocator:
    """Allocates stable host ports from the configured range, persisted in `port_allocations`."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: SettingsStore,
        docker: DockerEngine,
        probe: HostListenerProbeLike | None = None,
    ) -> None:
        self._sf = session_factory
        self._settings = settings
        self._docker = docker
        self._probe = probe
        self._lock = asyncio.Lock()

    async def _container_ports(self, ignore_container_id: str | None = None) -> set[Key]:
        used: set[Key] = set()
        for info in await self._docker.list_containers(all=True):
            if ignore_container_id and info.id == ignore_container_id:
                continue
            used.update((b.host_port, b.protocol) for b in info.published_ports)
        return used

    async def _external_in_use(self) -> set[Key]:
        used = await self._container_ports()
        if self._probe is not None:
            used |= await self._probe.listening()
        for port in (await self._settings.get()).reserved_ports:
            used.add((port, "tcp"))
            used.add((port, "udp"))
        return used

    async def in_use(self) -> set[tuple[int, Protocol_]]:
        """Every (host_port, protocol) that is allocated, published, listening or reserved."""
        used = await self._external_in_use()
        async with self._sf() as s:
            rows = (await s.execute(select(PortAllocationRow))).scalars().all()
        used.update((r.host_port, _proto(r)) for r in rows)
        return used

    async def allocate(
        self, app_id: int, requests: Sequence[tuple[int, Protocol_, int | None]]
    ) -> list[PortAllocation]:
        """Return stable allocations for `requests`, creating missing ones and dropping stale ones."""
        async with self._lock:
            for attempt in range(MAX_ATTEMPTS):
                try:
                    return await self._allocate_once(app_id, requests)
                except IntegrityError:
                    if attempt == MAX_ATTEMPTS - 1:
                        raise
        raise AssertionError("unreachable")  # pragma: no cover

    async def _allocate_once(
        self, app_id: int, requests: Sequence[tuple[int, Protocol_, int | None]]
    ) -> list[PortAllocation]:
        external = await self._external_in_use()
        settings = await self._settings.get()
        lo, hi = settings.port_range_start, settings.port_range_end
        wanted = {(cp, proto) for cp, proto, _ in requests}

        async with self._sf() as s, s.begin():
            rows = (await s.execute(select(PortAllocationRow))).scalars().all()
            mine = {(r.container_port, r.protocol): r for r in rows if r.app_id == app_id}
            used: set[Key] = set(external)
            used.update((r.host_port, _proto(r)) for r in rows)

            for key, row in list(mine.items()):
                if key not in wanted:
                    await s.delete(row)
                    used.discard((row.host_port, _proto(row)))
                    del mine[key]
            await s.flush()

            groups: dict[int, dict[Protocol_, int | None]] = {}
            for cp, proto, preferred in requests:
                if (cp, proto) not in mine:
                    groups.setdefault(cp, {}).setdefault(proto, preferred)

            for cp, protos in groups.items():
                candidates = [r.host_port for (c, _), r in mine.items() if c == cp]
                candidates += [p for p in protos.values() if p is not None]
                host = self._pick(candidates, list(protos), used, lo, hi)
                for proto in protos:
                    s.add(PortAllocationRow(app_id=app_id, container_port=cp, protocol=proto, host_port=host))
                    used.add((host, proto))
            await s.flush()

            result = (
                (await s.execute(select(PortAllocationRow).where(PortAllocationRow.app_id == app_id)))
                .scalars()
                .all()
            )
            by_key = {(r.container_port, r.protocol): _to_alloc(r) for r in result}

        out: list[PortAllocation] = []
        seen: set[tuple[int, str]] = set()
        for cp, proto, _ in requests:
            if (cp, proto) not in seen:
                seen.add((cp, proto))
                out.append(by_key[(cp, proto)])
        return out

    @staticmethod
    def _pick(candidates: list[int], protos: list[Protocol_], used: set[Key], lo: int, hi: int) -> int:
        for port in candidates:
            if lo <= port <= hi and all((port, p) not in used for p in protos):
                return port
        for port in range(lo, hi + 1):
            if all((port, p) not in used for p in protos):
                return port
        raise PortRangeExhausted(f"no free port in range {lo}-{hi}")

    async def pin(
        self,
        app_id: int,
        bindings: Sequence[tuple[int, Protocol_, int]],
        ignore_container_id: str | None = None,
    ) -> list[PortAllocation]:
        """Store exact host ports (possibly outside the range) for `app_id`."""
        foreign = await self._container_ports(ignore_container_id)
        async with self._lock:
            try:
                async with self._sf() as s, s.begin():
                    rows = (await s.execute(select(PortAllocationRow))).scalars().all()
                    others = {(r.host_port, _proto(r)) for r in rows if r.app_id != app_id}
                    for _, proto, host in bindings:
                        if (host, proto) in foreign or (host, proto) in others:
                            raise ConflictError(f"host port {host}/{proto} is already in use")
                    mine = {(r.container_port, r.protocol): r for r in rows if r.app_id == app_id}
                    for cp, proto, host in bindings:
                        row = mine.get((cp, proto))
                        if row is None:
                            s.add(
                                PortAllocationRow(
                                    app_id=app_id,
                                    container_port=cp,
                                    protocol=proto,
                                    host_port=host,
                                    pinned=True,
                                )
                            )
                        else:
                            row.host_port = host
                            row.pinned = True
                    await s.flush()
                    wanted = {(cp, proto) for cp, proto, _ in bindings}
                    result = (
                        (await s.execute(select(PortAllocationRow).where(PortAllocationRow.app_id == app_id)))
                        .scalars()
                        .all()
                    )
                    return [_to_alloc(r) for r in result if (r.container_port, r.protocol) in wanted]
            except IntegrityError as exc:
                raise ConflictError("host port conflicts with an existing allocation") from exc

    async def release(self, app_id: int) -> None:
        """Delete every allocation belonging to `app_id`."""
        async with self._sf() as s, s.begin():
            await s.execute(delete(PortAllocationRow).where(PortAllocationRow.app_id == app_id))
