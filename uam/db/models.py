"""SQLAlchemy table definitions. Keep in sync with the Alembic migrations (a test checks this)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from uam.db.types import UTCDateTime


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class CredentialRow(Base):
    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    username: Mapped[str] = mapped_column(String(200), default="")
    registry_host: Mapped[str] = mapped_column(String(255), default="")
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class SourceRow(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner: Mapped[str] = mapped_column(String(100))
    repo: Mapped[str] = mapped_column(String(100))
    ref: Mapped[str] = mapped_column(String(255), default="main")
    template_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AppRow(Base):
    __tablename__ = "apps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    template_path: Mapped[str] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(200), unique=True)
    overrides_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    image_tag_override: Mapped[str | None] = mapped_column(String(200), nullable=True)
    auto_ports: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_update_image: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_update_template: Mapped[bool] = mapped_column(Boolean, default=False)
    proxy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(32))
    installed_template_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    installed_image_digest: Mapped[str | None] = mapped_column(String(100), nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    available_template_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    available_image_digest: Mapped[str | None] = mapped_column(String(100), nullable=True)
    needs_attention: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_change_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    labels_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class PortAllocationRow(Base):
    __tablename__ = "port_allocations"
    __table_args__ = (
        UniqueConstraint("host_port", "protocol", name="uq_port_host_protocol"),
        UniqueConstraint("app_id", "container_port", "protocol", name="uq_port_app_container"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True)
    container_port: Mapped[int] = mapped_column(Integer)
    protocol: Mapped[str] = mapped_column(String(3))
    host_port: Mapped[int] = mapped_column(Integer)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)


class SettingsRow(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class KeyValueRow(Base):
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    app_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # no FK: outlives apps
    kind: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)


class HttpCacheRow(Base):
    __tablename__ = "http_cache"

    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    etag: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
