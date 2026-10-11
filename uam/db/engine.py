"""Database engine, session factory and migrations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def sync_url(url: str) -> str:
    """Translate an async SQLAlchemy URL into its sync equivalent for Alembic."""
    return url.replace("+aiosqlite", "")


def alembic_config(url: str) -> Config:
    """An Alembic config that works without alembic.ini."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", sync_url(url))
    return cfg


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class Database:
    """Holds the async engine and session factory."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", _enable_sqlite_foreign_keys)
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    async def dispose(self) -> None:
        """Close all connections."""
        await self.engine.dispose()


async def migrate(url: str) -> None:
    """Upgrade the database at `url` to the latest migration."""
    await asyncio.to_thread(command.upgrade, alembic_config(url), "head")


async def init(url: str) -> Database:
    """Run migrations and return a ready `Database`."""
    await migrate(url)
    return Database(url)
