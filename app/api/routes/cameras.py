"""Camera fleet registry."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.api.deps import AdminUser, CurrentUser, DbSession, Org, enforce_rate_limit
from app.core.errors import Conflict, NotFound
from app.models.entities import Camera, Capture
from app.schemas.models import CameraCreate, CameraOut
from app.services import audit

router = APIRouter(prefix="/cameras", tags=["cameras"], dependencies=[Depends(enforce_rate_limit)])


@router.get("", response_model=list[CameraOut])
def list_cameras(db: DbSession, user: CurrentUser, org: Org) -> list[CameraOut]:
    rows = db.scalars(
        select(Camera).where(Camera.org_id == org.id).order_by(Camera.code.asc())
    ).all()
    return [CameraOut.model_validate(r) for r in rows]


@router.get("/health")
def fleet_health(db: DbSession, user: CurrentUser, org: Org) -> dict:
    rows = db.scalars(select(Camera).where(Camera.org_id == org.id)).all()
    counts = {"online": 0, "degraded": 0, "offline": 0}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    last_seen = max((r.last_seen_at for r in rows if r.last_seen_at), default=None)
    return {
        "total": len(rows),
        "by_status": counts,
        "uptime_pct": round(100 * counts.get("online", 0) / len(rows), 1) if rows else 0.0,
        "last_seen_at": last_seen.isoformat() if last_seen else None,
    }


@router.post("", response_model=CameraOut, status_code=201)
def create_camera(
    payload: CameraCreate, db: DbSession, user: AdminUser, org: Org
) -> CameraOut:
    existing = db.scalars(
        select(Camera).where(Camera.org_id == org.id, Camera.code == payload.code)
    ).first()
    if existing:
        raise Conflict("A camera with this code already exists.", code="duplicate_camera")
    camera = Camera(
        org_id=org.id,
        code=payload.code.upper(),
        name=payload.name,
        location=payload.location,
        corridor=payload.corridor,
        lanes=payload.lanes,
        fps=payload.fps,
        status="online",
        last_seen_at=datetime.now(UTC),
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    audit.record(
        db, org_id=org.id, actor=user.email, action="camera.created",
        entity_type="camera", entity_id=camera.id, detail={"code": camera.code},
    )
    return CameraOut.model_validate(camera)


@router.patch("/{camera_id}/status", response_model=CameraOut)
def set_status(
    camera_id: str,
    db: DbSession,
    user: AdminUser,
    org: Org,
    status: str = Query(..., pattern="^(online|degraded|offline)$"),
) -> CameraOut:
    camera = db.scalars(
        select(Camera).where(Camera.id == camera_id, Camera.org_id == org.id)
    ).first()
    if camera is None:
        raise NotFound("Camera not found.", code="camera_not_found")
    camera.status = status
    camera.last_seen_at = datetime.now(UTC)
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return CameraOut.model_validate(camera)


@router.get("/{camera_id}/stats")
def camera_stats(camera_id: str, db: DbSession, user: CurrentUser, org: Org) -> dict:
    camera = db.scalars(
        select(Camera).where(Camera.id == camera_id, Camera.org_id == org.id)
    ).first()
    if camera is None:
        raise NotFound("Camera not found.", code="camera_not_found")
    scans = db.scalar(
        select(func.count(Capture.id)).where(Capture.camera_id == camera.id)
    )
    flagged = db.scalar(
        select(func.count(Capture.id)).where(
            Capture.camera_id == camera.id, Capture.status == "flagged"
        )
    )
    return {
        "camera_id": camera.id,
        "scans": int(scans or 0),
        "flagged": int(flagged or 0),
        "status": camera.status,
        "last_seen_at": camera.last_seen_at.isoformat() if camera.last_seen_at else None,
    }
