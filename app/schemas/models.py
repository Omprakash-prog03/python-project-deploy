"""Pydantic request/response contracts (drives OpenAPI docs at /docs)."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


# ----------------------------------------------------------------------- auth
class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=4, max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_RE.match(value):
            raise ValueError("not a valid email address")
        return value


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    full_name: str
    role: str
    avatar_initials: str
    zone: str
    org_id: str
    organization: str | None = None
    plan: str | None = None
    quota_used: int | None = None
    quota_total: int | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


# ------------------------------------------------------------------- captures
class GlyphOut(BaseModel):
    label: str
    confidence: float
    box: dict[str, int]


class PlateOut(BaseModel):
    plate: str
    raw_text: str = ""
    confidence: float
    box: dict[str, int]
    quad: list[list[float]] | None = None
    country: str = "GENERIC"
    region: str | None = None
    plate_type: str = "private"
    format: str | None = None
    format_valid: bool = False
    alternates: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    glyphs: list[GlyphOut] = Field(default_factory=list)


class RecognitionOut(BaseModel):
    plates: list[PlateOut]
    best: PlateOut | None = None
    image: dict[str, int]
    processing_ms: float
    timings: dict[str, float] = Field(default_factory=dict)
    engine_version: str
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class CaptureOut(BaseModel):
    id: str
    plate: str | None
    plate_display: str | None
    country: str
    region: str | None
    region_name: str
    plate_type: str
    confidence: float
    status: str
    risk_level: str
    risk_score: float
    watchlist_hit: bool
    watchlist_label: str | None = None
    image_url: str
    thumbnail_url: str
    image: dict[str, int]
    camera_id: str | None
    camera_name: str | None
    corridor: str | None
    operator: str | None
    source: str
    processing_ms: float
    engine_version: str
    notes: str | None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    alternates: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    lat: float | None = None
    lon: float | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CaptureListOut(BaseModel):
    items: list[CaptureOut]
    total: int
    page: int
    page_size: int
    pages: int


class CaptureUpdate(BaseModel):
    status: Literal["pending", "verified", "flagged", "denied", "archived"] | None = None
    notes: str | None = Field(default=None, max_length=2000)
    plate: str | None = Field(default=None, max_length=16)

    @field_validator("plate")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return v.strip().upper() if v else v


class TeachIn(BaseModel):
    """A corrected reading that also trains the glyph classifier."""

    plate: str = Field(min_length=3, max_length=16)

    @field_validator("plate")
    @classmethod
    def _normalise(cls, v: str) -> str:
        cleaned = "".join(ch for ch in v.upper() if ch.isalnum())
        if len(cleaned) < 3:
            raise ValueError("plate must contain at least 3 alphanumeric characters")
        return cleaned


class TeachOut(BaseModel):
    capture: CaptureOut
    learned: dict[str, int]
    stats: dict[str, Any]


# ------------------------------------------------------------------ watchlist
class WatchlistCreate(BaseModel):
    plate: str = Field(min_length=3, max_length=16)
    label: str = Field(default="", max_length=120)
    category: Literal["stolen", "banned", "vip", "permit", "unpaid", "other"] = "stolen"
    severity: Literal["low", "medium", "high", "critical"] = "high"
    reason: str = Field(default="", max_length=500)
    expires_at: datetime | None = None

    @field_validator("plate")
    @classmethod
    def _normalise(cls, v: str) -> str:
        cleaned = "".join(ch for ch in v.upper() if ch.isalnum())
        if len(cleaned) < 3:
            raise ValueError("plate must contain at least 3 alphanumeric characters")
        return cleaned


class WatchlistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    plate: str
    label: str
    category: str
    severity: str
    reason: str
    active: bool
    hits: int
    last_hit_at: datetime | None
    expires_at: datetime | None
    created_at: datetime


# -------------------------------------------------------------------- cameras
class CameraOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    name: str
    location: str
    corridor: str
    status: str
    lanes: int
    fps: float
    firmware: str
    last_seen_at: datetime | None
    created_at: datetime


class CameraCreate(BaseModel):
    code: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=2, max_length=120)
    location: str = Field(default="", max_length=160)
    corridor: str = Field(default="", max_length=80)
    lanes: int = Field(default=2, ge=1, le=12)
    fps: float = Field(default=25.0, gt=0, le=120)


# ------------------------------------------------------------------ analytics
class KpiBlock(BaseModel):
    scans: int
    unique_plates: int
    flagged: int
    watchlist_hits: int
    accuracy: float
    avg_confidence: float
    avg_processing_ms: float
    manual_review: int
    trend: dict[str, float] = Field(default_factory=dict)


class SeriesPoint(BaseModel):
    label: str
    scans: int
    flagged: int
    avg_confidence: float


class BreakdownItem(BaseModel):
    label: str
    value: int
    share: float


class OverviewOut(BaseModel):
    range: str
    kpis: KpiBlock
    series: list[SeriesPoint]
    corridors: list[BreakdownItem]
    plate_types: list[BreakdownItem]
    risk_mix: list[BreakdownItem]
    status_mix: list[BreakdownItem]
    recent: list[CaptureOut]
    watchlist: list[WatchlistOut]
    cameras_online: int
    cameras_total: int


# ------------------------------------------------------------------ audit/api
class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    actor: str
    action: str
    entity_type: str
    entity_id: str
    detail: dict[str, Any] | None
    ip: str | None
    created_at: datetime


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    scopes: list[str] = Field(default_factory=lambda: ["captures:read", "captures:write"])


class ApiKeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    scopes: list[str]
    active: bool
    last_used_at: datetime | None
    created_at: datetime
    token: str | None = None


class WebhookCreate(BaseModel):
    url: str = Field(max_length=500)
    description: str = Field(default="", max_length=160)
    events: list[str] = Field(default_factory=lambda: ["capture.created", "watchlist.hit"])


class WebhookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    url: str
    description: str
    events: list[str]
    active: bool
    failure_count: int
    last_status: int | None
    last_delivery_at: datetime | None
    created_at: datetime


class HealthOut(BaseModel):
    status: str
    version: str
    environment: str
    database: str
    storage: str
    engine: str
    uptime_seconds: float
    checks: dict[str, Any]
