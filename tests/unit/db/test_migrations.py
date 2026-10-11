from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from uam.db import migrate
from uam.db.models import Base


async def test_migrations_match_models(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'm.sqlite3'}"
    await migrate(url)
    engine = create_engine(f"sqlite:///{tmp_path / 'm.sqlite3'}")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []


async def test_migrate_is_idempotent(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'm.sqlite3'}"
    await migrate(url)
    await migrate(url)
