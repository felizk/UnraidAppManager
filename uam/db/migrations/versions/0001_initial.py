"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import uam.db.types

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, nullable: bool = False) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(name, uam.db.types.UTCDateTime(), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("username", sa.String(200), nullable=False),
        sa.Column("registry_host", sa.String(255), nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner", sa.String(100), nullable=False),
        sa.Column("repo", sa.String(100), nullable=False),
        sa.Column("ref", sa.String(255), nullable=False),
        sa.Column("template_path", sa.String(500), nullable=True),
        sa.Column(
            "credential_id", sa.Integer(), sa.ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
        ),
        _ts("created_at"),
    )
    op.create_table(
        "apps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id", sa.Integer(), sa.ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("template_path", sa.String(500), nullable=False),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("overrides_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("image_tag_override", sa.String(200), nullable=True),
        sa.Column("auto_ports", sa.Boolean(), nullable=False),
        sa.Column("auto_update_image", sa.Boolean(), nullable=False),
        sa.Column("auto_update_template", sa.Boolean(), nullable=False),
        sa.Column("proxy", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("installed_template_commit", sa.String(64), nullable=True),
        sa.Column("installed_image_digest", sa.String(100), nullable=True),
        sa.Column("container_id", sa.String(100), nullable=True),
        sa.Column("available_template_commit", sa.String(64), nullable=True),
        sa.Column("available_image_digest", sa.String(100), nullable=True),
        sa.Column("needs_attention", sa.Text(), nullable=True),
        _ts("last_change_at", nullable=True),
        _ts("last_checked_at", nullable=True),
        _ts("next_check_at", nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("labels_pending", sa.Boolean(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_apps_next_check_at", "apps", ["next_check_at"])
    op.create_table(
        "port_allocations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("app_id", sa.Integer(), sa.ForeignKey("apps.id", ondelete="CASCADE"), nullable=False),
        sa.Column("container_port", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(3), nullable=False),
        sa.Column("host_port", sa.Integer(), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("host_port", "protocol", name="uq_port_host_protocol"),
        sa.UniqueConstraint("app_id", "container_port", "protocol", name="uq_port_app_container"),
    )
    op.create_index("ix_port_allocations_app_id", "port_allocations", ["app_id"])
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_table(
        "kv",
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        _ts("ts"),
        sa.Column("app_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
    )
    op.create_index("ix_events_ts", "events", ["ts"])
    op.create_index("ix_events_app_id", "events", ["app_id"])
    op.create_table(
        "http_cache",
        sa.Column("key", sa.String(512), primary_key=True),
        sa.Column("etag", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        _ts("updated_at"),
    )


def downgrade() -> None:
    for table in (
        "http_cache",
        "events",
        "kv",
        "settings",
        "port_allocations",
        "apps",
        "sources",
        "credentials",
    ):
        op.drop_table(table)
