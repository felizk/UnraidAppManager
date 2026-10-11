"""Shared fixtures: frozen time, a migrated temp database, the fake bundle and fixture templates."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import time_machine

from tests.fakes import FakeBundle
from uam.db import Database, init

FIXTURES = Path(__file__).parent / "fixtures"
FROZEN_AT = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


class FrozenClock:
    """Callable clock (`clock()` → aware UTC now) that also freezes `datetime.now()` and `time.time()`."""

    def __init__(self, traveller: Any) -> None:
        self._t = traveller

    def __call__(self) -> datetime:
        return datetime.now(UTC)

    @property
    def now(self) -> datetime:
        return datetime.now(UTC)

    def advance(self, delta: timedelta) -> datetime:
        """Move time forward and return the new now."""
        self._t.shift(delta)
        return self.now

    def set(self, when: datetime) -> None:
        self._t.move_to(when)


@pytest.fixture
def frozen_now() -> Iterator[FrozenClock]:
    with time_machine.travel(FROZEN_AT, tick=False) as traveller:
        yield FrozenClock(traveller)


@pytest.fixture
async def tmp_db(tmp_path: Path) -> AsyncIterator[Database]:
    db = await init(f"sqlite+aiosqlite:///{tmp_path / 'uam.sqlite3'}")
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
def fakes() -> FakeBundle:
    return FakeBundle()


def load_template(name: str) -> str:
    """Contents of tests/fixtures/templates/<name> (e.g. "simple-web.xml")."""
    return (FIXTURES / "templates" / name).read_text()


@pytest.fixture
def template_xml() -> Callable[[str], str]:
    """`template_xml("simple-web.xml")` → XML text."""
    return load_template


TEMPLATE_FIXTURES = sorted(p.name for p in (FIXTURES / "templates").glob("*.xml"))
