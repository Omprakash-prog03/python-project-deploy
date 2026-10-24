"""ORM entities for the Plate Intelligence platform."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Organization(Base, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    plan: Mapped[str] = mapped_column(String(32), default="enterprise")
    monthly_scan_quota: Mapped[int] = mapped_column(Integer, default=250_000)
    brand_accent: Mapped[str] = mapped_column(String(16), default="#C9B8FF")

    users: Mapped[list[User]] = relationship(back_populates="organization")
    cameras: Mapped[list[Camera]] = relationship(back_populates="organization")


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(24), default="operator")  # admin|supervisor|operator|viewer
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_initials: Mapped[str] = mapped_column(String(4), default="OP")
    zone: Mapped[str] = mapped_column(String(60), default="North Corridor")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organization: Mapped[Organization] = relationship(back_populates="users")


class Camera(Base, TimestampMixin):
    __tablename__ = "cameras"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    location: Mapped[str] = mapped_column(String(160), default="")
    corridor: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(16), default="online")  # online|degraded|offline
    lanes: Mapped[int] = mapped_column(Integer, default=2)
    fps: Mapped[float] = mapped_column(Float, default=25.0)
    firmware: Mapped[str] = mapped_column(String(32), default="pv-eye 2.4.1")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organization: Mapped[Organization] = relationship(back_populates="cameras")

    __table_args__ = (UniqueConstraint("org_id", "code", name="uq_camera_org_code"),)


class Capture(Base, TimestampMixin):
    """One recognition event (an uploaded frame + its inference result)."""

    __tablename__ = "captures"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    camera_id: Mapped[str | None] = mapped_column(ForeignKey("cameras.id", ondelete="SET NULL"), index=True)

    plate: Mapped[str | None] = mapped_column(String(24), index=True)
    plate_display: Mapped[str | None] = mapped_column(String(32))
    country: Mapped[str] = mapped_column(String(8), default="IN")
    region: Mapped[str | None] = mapped_column(String(12), index=True)
    plate_type: Mapped[str] = mapped_column(String(24), default="private")

    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # pending | verified | flagged | denied | archived
    risk_level: Mapped[str] = mapped_column(String(12), default="low", index=True)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    watchlist_hit: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    image_path: Mapped[str] = mapped_column(String(255), nullable=False)
    thumbnail_path: Mapped[str | None] = mapped_column(String(255))
    image_width: Mapped[int] = mapped_column(Integer, default=0)
    image_height: Mapped[int] = mapped_column(Integer, default=0)
    image_bytes: Mapped[int] = mapped_column(Integer, default=0)

    source: Mapped[str] = mapped_column(String(24), default="upload")  # upload|api|stream
    processing_ms: Mapped[float] = mapped_column(Float, default=0.0)
    engine_version: Mapped[str] = mapped_column(String(24), default="pv-alpr/1.0")
    candidates: Mapped[list | None] = mapped_column(JSON, default=list)
    alternates: Mapped[list | None] = mapped_column(JSON, default=list)
    diagnostics: Mapped[dict | None] = mapped_column(JSON, default=dict)
    notes: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    reviewed_by: Mapped[str | None] = mapped_column(String(120))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    camera: Mapped[Camera | None] = relationship()
    user: Mapped[User | None] = relationship()

    __table_args__ = (
        Index("ix_capture_org_created", "org_id", "created_at"),
        Index("ix_capture_org_plate", "org_id", "plate"),
    )


class WatchlistEntry(Base, TimestampMixin):
    __tablename__ = "watchlist"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    plate: Mapped[str] = mapped_column(String(24), index=True, nullable=False)
    label: Mapped[str] = mapped_column(String(120), default="")
    category: Mapped[str] = mapped_column(String(40), default="stolen")  # stolen|banned|vip|permit|unpaid
    severity: Mapped[str] = mapped_column(String(12), default="high")  # low|medium|high|critical
    reason: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(120))
    hits: Mapped[int] = mapped_column(Integer, default=0)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("org_id", "plate", name="uq_watchlist_org_plate"),)


class AuditEvent(Base, TimestampMixin):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    actor: Mapped[str] = mapped_column(String(160), default="system")
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(32), default="")
    entity_id: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict | None] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(32))


class WebhookEndpoint(Base, TimestampMixin):
    __tablename__ = "webhook_endpoints"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    secret: Mapped[str] = mapped_column(String(120), nullable=False)
    events: Mapped[list | None] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str] = mapped_column(String(160), default="")
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_status: Mapped[int | None] = mapped_column(Integer)
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiKey(Base, TimestampMixin):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    prefix: Mapped[str] = mapped_column(String(12), index=True)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes: Mapped[list | None] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
