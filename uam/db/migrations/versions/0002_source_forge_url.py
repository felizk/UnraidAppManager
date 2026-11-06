"""sources.forge_url for Forgejo/Gitea sources

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the column; existing sources stay on GitHub."""
    with op.batch_alter_table("sources") as batch:
        batch.add_column(sa.Column("forge_url", sa.String(255), nullable=False, server_default=""))


def downgrade() -> None:
    """Drop the column."""
    with op.batch_alter_table("sources") as batch:
        batch.drop_column("forge_url")
