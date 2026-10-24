"""Capture (recognition event) endpoints."""
from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession, Org, client_ip, enforce_rate_limit
from app.core.errors import Forbidden, NotFound, ValidationFailed
from app.models.entities import Capture, WatchlistEntry
from app.schemas.models import CaptureListOut, CaptureOut, CaptureUpdate
from app.services import audit, recognition
from app.services.serializers import capture_out
from app.services.storage import storage

router = APIRouter(prefix="/captures", tags=["captures"], dependencies=[Depends(enforce_rate_limit)])


def _watchlist_lookup(db, org_id: str, plates: set[str]) -> dict[str, WatchlistEntry]:
    if not plates:
        return {}
    entries = db.scalars(
        select(WatchlistEntry).where(
            WatchlistEntry.org_id == org_id, WatchlistEntry.plate.in_(plates)
        )
    ).all()
    return {e.plate: e for e in entries}


@router.post("", response_model=CaptureOut, status_code=201)
async def create_capture(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    org: Org,
    file: Annotated[UploadFile, File(description="Vehicle frame (jpg/png/webp, max 12 MB)")],
    camera_id: Annotated[str | None, Form()] = None,
    country: Annotated[str | None, Form()] = None,
    notes: Annotated[str | None, Form()] = None,
    source: Annotated[str, Form()] = "upload",
    lat: Annotated[float | None, Form()] = None,
    lon: Annotated[float | None, Form()] = None,
) -> CaptureOut:
    data = await file.read()
    if len(data) > 16 * 1024 * 1024:
        raise ValidationFailed("Upload exceeds the hard limit of 16 MB.", code="upload_too_large")
    capture, _ = recognition.ingest(
        db,
        org=org,
        user=user,
        data=data,
        filename=file.filename or "upload.jpg",
        camera_id=camera_id,
        country=country,
        notes=notes,
        source=source if source in ("upload", "api", "stream") else "upload",
        lat=lat,
        lon=lon,
    )
    entry = _watchlist_lookup(db, org.id, {capture.plate} if capture.plate else set())
    return CaptureOut(**capture_out(capture, watchlist=entry.get(capture.plate or "")))


@router.post("/batch", response_model=list[CaptureOut], status_code=201)
async def create_batch(
    db: DbSession,
    user: CurrentUser,
    org: Org,
    files: Annotated[list[UploadFile], File(description="Up to 10 frames")],
    camera_id: Annotated[str | None, Form()] = None,
) -> list[CaptureOut]:
    if not files:
        raise ValidationFailed("No files supplied.", code="empty_upload")
    if len(files) > 10:
        raise ValidationFailed("Batch uploads are limited to 10 frames.", code="batch_too_large")
    results: list[CaptureOut] = []
    for upload in files:
        data = await upload.read()
        capture, _ = recognition.ingest(
            db,
            org=org,
            user=user,
            data=data,
            filename=upload.filename or "frame.jpg",
            camera_id=camera_id,
            source="stream",
        )
        results.append(CaptureOut(**capture_out(capture)))
    return results


@router.get("", response_model=CaptureListOut)
def list_captures(
    db: DbSession,
    user: CurrentUser,
    org: Org,
    q: Annotated[str | None, Query(max_length=32, description="Plate fragment search")] = None,
    status: Annotated[str | None, Query()] = None,
    risk: Annotated[str | None, Query()] = None,
    camera_id: Annotated[str | None, Query()] = None,
    plate_type: Annotated[str | None, Query()] = None,
    watchlist_only: bool = Query(False),
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    sort: Literal["recent", "confidence", "risk"] = Query("recent"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> CaptureListOut:
    stmt = (
        select(Capture)
        .options(selectinload(Capture.camera), selectinload(Capture.user))
        .where(Capture.org_id == org.id)
    )
    if q:
        needle = "".join(ch for ch in q.upper() if ch.isalnum())
        if needle:
            stmt = stmt.where(
                or_(Capture.plate.contains(needle), Capture.plate_display.contains(needle))
            )
    if status and status != "all":
        stmt = stmt.where(Capture.status == status)
    if risk and risk != "all":
        stmt = stmt.where(Capture.risk_level == risk)
    if camera_id:
        stmt = stmt.where(Capture.camera_id == camera_id)
    if plate_type and plate_type != "all":
        stmt = stmt.where(Capture.plate_type == plate_type)
    if watchlist_only:
        stmt = stmt.where(Capture.watchlist_hit.is_(True))
    if date_from:
        stmt = stmt.where(Capture.created_at >= date_from)
    if date_to:
        stmt = stmt.where(Capture.created_at <= date_to)

    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    order = {
        "recent": Capture.created_at.desc(),
        "confidence": Capture.confidence.desc(),
        "risk": Capture.risk_score.desc(),
    }[sort]
    rows = db.scalars(stmt.order_by(order).offset((page - 1) * page_size).limit(page_size)).all()
    watchlist = _watchlist_lookup(db, org.id, {c.plate for c in rows if c.plate})
    pages = max(1, (total + page_size - 1) // page_size)
    return CaptureListOut(
        items=[CaptureOut(**capture_out(c, watchlist=watchlist.get(c.plate or ""))) for c in rows],
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@router.get("/export.csv")
def export_captures(
    db: DbSession,
    user: CurrentUser,
    org: Org,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
) -> StreamingResponse:
    stmt = (
        select(Capture)
        .options(selectinload(Capture.camera))
        .where(Capture.org_id == org.id)
        .order_by(Capture.created_at.desc())
        .limit(10_000)
    )
    if date_from:
        stmt = stmt.where(Capture.created_at >= date_from)
    if date_to:
        stmt = stmt.where(Capture.created_at <= date_to)
    rows = db.scalars(stmt).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "capture_id", "plate", "status", "risk_level", "confidence", "country", "region",
        "plate_type", "camera", "corridor", "source", "processing_ms", "created_at",
    ])
    for row in rows:
        writer.writerow([
            row.id, row.plate, row.status, row.risk_level, f"{row.confidence:.4f}", row.country,
            row.region, row.plate_type, row.camera.name if row.camera else "",
            row.camera.corridor if row.camera else "", row.source,
            f"{row.processing_ms:.1f}", row.created_at.isoformat(),
        ])
    buffer.seek(0)
    audit.record(
        db, org_id=org.id, actor=user.email, action="captures.exported",
        detail={"count": len(rows)}, ip="api",
    )
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="platevision-captures-'
                f'{datetime.now(UTC):%Y%m%d-%H%M}.csv"'
            )
        },
    )


@router.get("/{capture_id}", response_model=CaptureOut)
def get_capture(capture_id: str, db: DbSession, user: CurrentUser, org: Org) -> CaptureOut:
    capture = db.scalars(
        select(Capture)
        .options(selectinload(Capture.camera), selectinload(Capture.user))
        .where(Capture.id == capture_id, Capture.org_id == org.id)
    ).first()
    if capture is None:
        raise NotFound("Capture not found.", code="capture_not_found")
    watchlist = _watchlist_lookup(db, org.id, {capture.plate} if capture.plate else set())
    return CaptureOut(**capture_out(capture, watchlist=watchlist.get(capture.plate or "")))


@router.get("/{capture_id}/image")
def capture_image(
    capture_id: str,
    db: DbSession,
    user: CurrentUser,
    org: Org,
    variant: Literal["full", "thumb"] = Query("full"),
) -> Response:
    capture = db.scalars(
        select(Capture).where(Capture.id == capture_id, Capture.org_id == org.id)
    ).first()
    if capture is None:
        raise NotFound("Capture not found.", code="capture_not_found")
    key = capture.thumbnail_path if variant == "thumb" and capture.thumbnail_path else capture.image_path
    data = storage.read(key)
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=86400, immutable",
            "Content-Length": str(len(data)),
        },
    )


@router.patch("/{capture_id}", response_model=CaptureOut)
def update_capture(
    capture_id: str,
    payload: CaptureUpdate,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    org: Org,
) -> CaptureOut:
    capture = db.scalars(
        select(Capture)
        .options(selectinload(Capture.camera), selectinload(Capture.user))
        .where(Capture.id == capture_id, Capture.org_id == org.id)
    ).first()
    if capture is None:
        raise NotFound("Capture not found.", code="capture_not_found")
    changes: dict[str, object] = {}
    if payload.status and payload.status != capture.status:
        if user.role == "viewer":
            raise Forbidden("Viewers cannot change capture status.", code="insufficient_role")
        changes["status"] = payload.status
        capture.status = payload.status
        capture.reviewed_by = user.full_name
        capture.reviewed_at = datetime.now(UTC)
    if payload.notes is not None:
        capture.notes = payload.notes
        changes["notes"] = "updated"
    if payload.plate and payload.plate != capture.plate:
        if user.role == "viewer":
            raise Forbidden("Viewers cannot correct plates.", code="insufficient_role")
        capture.plate = payload.plate
        capture.plate_display = payload.plate
        changes["plate"] = payload.plate
    db.add(capture)
    db.commit()
    db.refresh(capture)
    audit.record(
        db,
        org_id=org.id,
        actor=user.email,
        action="capture.updated",
        entity_type="capture",
        entity_id=capture.id,
        detail=changes,
        ip=client_ip(request),
    )
    watchlist = _watchlist_lookup(db, org.id, {capture.plate} if capture.plate else set())
    return CaptureOut(**capture_out(capture, watchlist=watchlist.get(capture.plate or "")))


@router.post("/{capture_id}/reanalyze", response_model=CaptureOut)
def reanalyze(capture_id: str, db: DbSession, user: CurrentUser, org: Org) -> CaptureOut:
    capture = db.scalars(
        select(Capture)
        .options(selectinload(Capture.camera), selectinload(Capture.user))
        .where(Capture.id == capture_id, Capture.org_id == org.id)
    ).first()
    if capture is None:
        raise NotFound("Capture not found.", code="capture_not_found")
    recognition.reanalyze(db, capture, org.id)
    return CaptureOut(**capture_out(capture))


@router.delete("/{capture_id}", status_code=204)
def delete_capture(capture_id: str, db: DbSession, user: CurrentUser, org: Org) -> Response:
    if user.role not in ("admin", "supervisor"):
        raise Forbidden("Only supervisors can delete evidence.", code="insufficient_role")
    capture = db.scalars(
        select(Capture).where(Capture.id == capture_id, Capture.org_id == org.id)
    ).first()
    if capture is None:
        raise NotFound("Capture not found.", code="capture_not_found")
    for key in (capture.image_path, capture.thumbnail_path):
        if key:
            storage.delete(key)
    db.delete(capture)
    db.commit()
    audit.record(
        db, org_id=org.id, actor=user.email, action="capture.deleted",
        entity_type="capture", entity_id=capture_id,
    )
    return Response(status_code=204)
