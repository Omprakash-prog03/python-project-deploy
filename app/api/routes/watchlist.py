"""Watchlist management (stolen / banned / VIP / permit enforcement)."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from app.alpr import grammar
from app.api.deps import CurrentUser, DbSession, Org, SupervisorUser, client_ip, enforce_rate_limit
from app.core.errors import Conflict, NotFound
from app.models.entities import Capture, WatchlistEntry
from app.schemas.models import WatchlistCreate, WatchlistOut
from app.services import audit
from app.services.serializers import watchlist_out

router = APIRouter(prefix="/watchlist", tags=["watchlist"], dependencies=[Depends(enforce_rate_limit)])


@router.get("", response_model=list[WatchlistOut])
def list_watchlist(
    db: DbSession,
    user: CurrentUser,
    org: Org,
    q: str | None = Query(None, max_length=32),
    category: str | None = Query(None),
    active_only: bool = Query(True),
    limit: int = Query(100, ge=1, le=500),
) -> list[WatchlistOut]:
    stmt = select(WatchlistEntry).where(WatchlistEntry.org_id == org.id)
    if q:
        needle = grammar.normalize(q)
        stmt = stmt.where(WatchlistEntry.plate.contains(needle))
    if category and category != "all":
        stmt = stmt.where(WatchlistEntry.category == category)
    if active_only:
        stmt = stmt.where(WatchlistEntry.active.is_(True))
    rows = db.scalars(stmt.order_by(WatchlistEntry.created_at.desc()).limit(limit)).all()
    return [WatchlistOut(**watchlist_out(r)) for r in rows]


@router.post("", response_model=WatchlistOut, status_code=201)
def add_watchlist(
    payload: WatchlistCreate,
    request: Request,
    db: DbSession,
    user: SupervisorUser,
    org: Org,
) -> WatchlistOut:
    existing = db.scalars(
        select(WatchlistEntry).where(
            WatchlistEntry.org_id == org.id, WatchlistEntry.plate == payload.plate
        )
    ).first()
    if existing:
        raise Conflict("This plate is already on the watchlist.", code="duplicate_watchlist_entry")
    entry = WatchlistEntry(
        org_id=org.id,
        plate=payload.plate,
        label=payload.label,
        category=payload.category,
        severity=payload.severity,
        reason=payload.reason,
        expires_at=payload.expires_at,
        created_by=user.full_name,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    audit.record(
        db,
        org_id=org.id,
        actor=user.email,
        action="watchlist.added",
        entity_type="watchlist",
        entity_id=entry.id,
        detail={"plate": entry.plate, "category": entry.category},
        ip=client_ip(request),
    )
    return WatchlistOut(**watchlist_out(entry))


@router.patch("/{entry_id}/toggle", response_model=WatchlistOut)
def toggle_entry(entry_id: str, db: DbSession, user: SupervisorUser, org: Org) -> WatchlistOut:
    entry = db.scalars(
        select(WatchlistEntry).where(WatchlistEntry.id == entry_id, WatchlistEntry.org_id == org.id)
    ).first()
    if entry is None:
        raise NotFound("Watchlist entry not found.", code="watchlist_not_found")
    entry.active = not entry.active
    db.add(entry)
    db.commit()
    db.refresh(entry)
    audit.record(
        db, org_id=org.id, actor=user.email, action="watchlist.toggled",
        entity_type="watchlist", entity_id=entry.id, detail={"active": entry.active},
    )
    return WatchlistOut(**watchlist_out(entry))


@router.delete("/{entry_id}", status_code=204)
def delete_entry(entry_id: str, db: DbSession, user: SupervisorUser, org: Org) -> None:
    entry = db.scalars(
        select(WatchlistEntry).where(WatchlistEntry.id == entry_id, WatchlistEntry.org_id == org.id)
    ).first()
    if entry is None:
        raise NotFound("Watchlist entry not found.", code="watchlist_not_found")
    db.delete(entry)
    db.commit()
    audit.record(
        db, org_id=org.id, actor=user.email, action="watchlist.removed",
        entity_type="watchlist", entity_id=entry_id,
    )


@router.get("/matches", response_model=list[dict])
def matches(
    db: DbSession,
    user: CurrentUser,
    org: Org,
    limit: int = Query(20, ge=1, le=100),
) -> list[dict]:
    rows = db.execute(
        select(Capture, WatchlistEntry)
        .join(WatchlistEntry, (WatchlistEntry.plate == Capture.plate) & (WatchlistEntry.org_id == Capture.org_id))
        .where(Capture.org_id == org.id, Capture.watchlist_hit.is_(True))
        .order_by(Capture.created_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "capture_id": capture.id,
            "plate": capture.plate,
            "camera": capture.camera.name if capture.camera else None,
            "category": entry.category,
            "severity": entry.severity,
            "label": entry.label,
            "created_at": capture.created_at.isoformat(),
            "image_url": f"/api/v1/captures/{capture.id}/image?variant=thumb",
        }
        for capture, entry in rows
    ]


@router.get("/stats")
def stats(db: DbSession, user: CurrentUser, org: Org) -> dict:
    total = db.scalar(
        select(func.count(WatchlistEntry.id)).where(WatchlistEntry.org_id == org.id)
    )
    active = db.scalar(
        select(func.count(WatchlistEntry.id)).where(
            WatchlistEntry.org_id == org.id, WatchlistEntry.active.is_(True)
        )
    )
    hits = db.scalar(
        select(func.count(Capture.id)).where(
            Capture.org_id == org.id, Capture.watchlist_hit.is_(True)
        )
    )
    return {
        "total": int(total or 0),
        "active": int(active or 0),
        "hits": int(hits or 0),
        "generated_at": datetime.now(UTC).isoformat(),
    }
