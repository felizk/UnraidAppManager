"""Alembic environment: synchronous migrations against the configured URL."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

from uam.db.engine import sync_url
from uam.db.models import Base

config = context.config
url = config.get_main_option("sqlalchemy.url") or sync_url(os.environ.get("UAM_DB_URL", ""))
config.set_main_option("sqlalchemy.url", url)


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=Base.metadata, render_as_batch=True, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
