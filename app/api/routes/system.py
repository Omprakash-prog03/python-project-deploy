"""Platform administration: health, metrics, audit, API keys and webhooks."""
from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select, text

from app.alpr import learning
from app.alpr.engine import get_engine
from app.api.deps import AdminUser, CurrentUser, DbSession, Org, SupervisorUser, enforce_rate_limit
from app.core.config import settings
from app.core.metrics import metrics
from app.core.security import hash_password
from app.models.entities import ApiKey, AuditEvent, Camera, Capture, User, WebhookEndpoint
from app.schemas.models import (
    ApiKeyCreate,
    ApiKeyOut,
    AuditOut,
    HealthOut,
    WebhookCreate,
    WebhookOut,
)
from app.services import audit
from app.services.storage import storage

router = APIRouter(tags=["system"], dependencies=[Depends(enforce_rate_limit)])
started_at = time.time()


@router.get("/health", response_model=HealthOut)
def health(db: DbSession) -> HealthOut:
    checks: dict[str, object] = {}
    database_ok = True
    try:
        db.execute(text("SELECT 1"))
        checks["database_latency_ms"] = 1
    except Exception as exc:
        database_ok = False
        checks["database_error"] = str(exc)

    storage_ok = True
    try:
        probe = storage.save_key(b"health", key="_health/probe.txt")
        storage.delete(probe[0])
        checks["storage_backend"] = storage.backend
    except Exception as exc:
        storage_ok = False
        checks["storage_error"] = str(exc)

    engine_ok = True
    try:
        engine = get_engine()
        engine.classifier._ensure()
        checks["glyph_classes"] = len(engine.classifier.bank.class_index)
        checks["engine_version"] = engine.version
    except Exception as exc:
        engine_ok = False
        checks["engine_error"] = str(exc)

    # Which reader actually answered is the first question when accuracy looks
    # wrong in production, so surface it in health rather than in a log file.
    checks["engine_backend"] = "local"
    checks["remote_reader"] = "off"
    if settings.parkpow_configured:
        checks["remote_reader"] = "on"
        checks["remote_reader_endpoint"] = settings.parkpow_base_url
    elif settings.alpr_engine == "parkpow":
        checks["remote_reader"] = "missing-key"
    checks["engine_backend"] = "parkpow" if settings.parkpow_configured else "local"

    status = "ok" if all([database_ok, storage_ok, engine_ok]) else "degraded"
    metrics.gauge("health_ok", 1.0 if status == "ok" else 0.0)
    return HealthOut(
        status=status,
        version=settings.version,
        environment=settings.environment,
        database="ok" if database_ok else "error",
        storage="ok" if storage_ok else "error",
        engine="ok" if engine_ok else "error",
        uptime_seconds=round(time.time() - started_at, 1),
        checks=checks,
    )


@router.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    return Response(content=metrics.prometheus(), media_type="text/plain; version=0.0.4")


@router.get("/metrics/summary")
def metrics_summary(user: CurrentUser) -> dict:
    return metrics.snapshot()


@router.get("/info")
def info(user: CurrentUser, db: DbSession, org: Org) -> dict:
    scans = db.scalar(select(func.count(Capture.id)).where(Capture.org_id == org.id)) or 0
    cameras = db.scalar(select(func.count(Camera.id)).where(Camera.org_id == org.id)) or 0
    return {
        "app": settings.app_name,
        "tagline": settings.app_tagline,
        "version": settings.version,
        "environment": settings.environment,
        "engine_version": get_engine().version,
        "organization": {"id": org.id, "name": org.name, "plan": org.plan},
        "usage": {
            "scans": int(scans),
            "quota": org.monthly_scan_quota,
            "utilization_pct": round(100 * scans / org.monthly_scan_quota, 2) if org.monthly_scan_quota else 0,
            "cameras": int(cameras),
        },
        "capabilities": {
            "countries": settings.country_list,
            "ocr_engine": settings.alpr_ocr_engine,
            "storage_backend": storage.backend,
            "review_threshold": settings.alpr_review_confidence,
        },
        "server_time": datetime.now(UTC).isoformat(),
    }


@router.get("/audit", response_model=list[AuditOut])
def audit_log(
    db: DbSession,
    user: AdminUser,
    org: Org,
    action: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[AuditOut]:
    stmt = select(AuditEvent).where(AuditEvent.org_id == org.id)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    rows = db.scalars(stmt.order_by(AuditEvent.created_at.desc()).limit(limit)).all()
    return [AuditOut.model_validate(r) for r in rows]


@router.get("/api-keys", response_model=list[ApiKeyOut])
def list_api_keys(db: DbSession, user: AdminUser, org: Org) -> list[ApiKeyOut]:
    rows = db.scalars(
        select(ApiKey).where(ApiKey.org_id == org.id).order_by(ApiKey.created_at.desc())
    ).all()
    return [
        ApiKeyOut(
            id=r.id, name=r.name, prefix=r.prefix, scopes=r.scopes or [], active=r.active,
            last_used_at=r.last_used_at, created_at=r.created_at, token=None,
        )
        for r in rows
    ]


@router.post("/api-keys", response_model=ApiKeyOut, status_code=201)
def create_api_key(
    payload: ApiKeyCreate, db: DbSession, user: AdminUser, org: Org
) -> ApiKeyOut:
    raw = f"pv_{secrets.token_urlsafe(32)}"
    key = ApiKey(
        org_id=org.id,
        name=payload.name,
        prefix=raw[:10],
        key_hash=hash_password(raw),
        scopes=payload.scopes,
        active=True,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    audit.record(
        db, org_id=org.id, actor=user.email, action="apikey.created",
        entity_type="api_key", entity_id=key.id, detail={"name": key.name},
    )
    return ApiKeyOut(
        id=key.id, name=key.name, prefix=key.prefix, scopes=key.scopes or [], active=key.active,
        last_used_at=None, created_at=key.created_at, token=raw,
    )


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_api_key(key_id: str, db: DbSession, user: AdminUser, org: Org) -> Response:
    key = db.scalars(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.org_id == org.id)
    ).first()
    if key:
        key.active = False
        db.add(key)
        db.commit()
    audit.record(
        db, org_id=org.id, actor=user.email, action="apikey.revoked",
        entity_type="api_key", entity_id=key_id,
    )
    return Response(status_code=204)


@router.get("/webhooks", response_model=list[WebhookOut])
def list_webhooks(db: DbSession, user: SupervisorUser, org: Org) -> list[WebhookOut]:
    rows = db.scalars(
        select(WebhookEndpoint).where(WebhookEndpoint.org_id == org.id)
    ).all()
    return [WebhookOut.model_validate(r) for r in rows]


@router.post("/webhooks", response_model=WebhookOut, status_code=201)
def create_webhook(
    payload: WebhookCreate, db: DbSession, user: AdminUser, org: Org
) -> WebhookOut:
    endpoint = WebhookEndpoint(
        org_id=org.id,
        url=payload.url,
        secret=secrets.token_urlsafe(24),
        events=payload.events,
        description=payload.description,
        active=True,
    )
    db.add(endpoint)
    db.commit()
    db.refresh(endpoint)
    audit.record(
        db, org_id=org.id, actor=user.email, action="webhook.created",
        entity_type="webhook", entity_id=endpoint.id, detail={"url": endpoint.url},
    )
    return WebhookOut.model_validate(endpoint)


@router.delete("/webhooks/{webhook_id}", status_code=204)
def delete_webhook(webhook_id: str, db: DbSession, user: AdminUser, org: Org) -> Response:
    endpoint = db.scalars(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == webhook_id, WebhookEndpoint.org_id == org.id
        )
    ).first()
    if endpoint:
        db.delete(endpoint)
        db.commit()
        audit.record(
            db, org_id=org.id, actor=user.email, action="webhook.deleted",
            entity_type="webhook", entity_id=webhook_id,
        )
    return Response(status_code=204)


@router.get("/learning")
def learning_stats(user: CurrentUser) -> dict:
    """What the reader has learned from operator corrections."""
    return learning.stats()


@router.delete("/learning", status_code=204)
def learning_reset(user: AdminUser) -> Response:
    """Forget every learned exemplar and fall back to the rendered bank."""
    learning.reset()
    return Response(status_code=204)


@router.get("/team", response_model=list[dict])
def team(db: DbSession, user: SupervisorUser, org: Org) -> list[dict]:
    rows = db.scalars(select(User).where(User.org_id == org.id).order_by(User.full_name)).all()
    return [
        {
            "id": r.id,
            "name": r.full_name,
            "email": r.email,
            "role": r.role,
            "initials": r.avatar_initials,
            "zone": r.zone,
            "last_login_at": r.last_login_at.isoformat() if r.last_login_at else None,
        }
        for r in rows
    ]
