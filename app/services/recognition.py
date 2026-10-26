"""Capture ingestion: storage, inference, policy checks and persistence."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.alpr import grammar, imaging
from app.alpr.engine import ENGINE_VERSION, EngineOptions, get_engine
from app.alpr.types import RecognitionResult
from app.core.config import settings
from app.core.errors import PayloadTooLarge, ValidationFailed
from app.core.logging import get_logger
from app.models.entities import Capture, Organization, User, WatchlistEntry
from app.services import audit, webhooks
from app.services.storage import storage

logger = get_logger(__name__)


def validate_upload(data: bytes) -> None:
    if not data:
        raise ValidationFailed("No image data received.", code="empty_upload")
    limit = settings.max_upload_mb * 1024 * 1024
    if len(data) > limit:
        raise PayloadTooLarge(
            f"Image exceeds the {settings.max_upload_mb} MB upload limit.",
            code="upload_too_large",
            details={"max_mb": settings.max_upload_mb},
        )


def lookup_watchlist(db: Session, org_id: str, plate: str | None) -> WatchlistEntry | None:
    if not plate:
        return None
    normalized = grammar.normalize(plate)
    entry = db.scalars(
        select(WatchlistEntry).where(
            WatchlistEntry.org_id == org_id,
            WatchlistEntry.plate == normalized,
            WatchlistEntry.active.is_(True),
        )
    ).first()
    if entry:
        return entry
    # Near-miss matching tolerant to a single misread character.
    candidates = db.scalars(
        select(WatchlistEntry).where(
            WatchlistEntry.org_id == org_id,
            WatchlistEntry.active.is_(True),
        ).limit(500)
    ).all()
    for candidate in candidates:
        if abs(len(candidate.plate) - len(normalized)) > 1:
            continue
        if _hamming(candidate.plate, normalized) <= 1 and len(normalized) >= 6:
            return candidate
    return None


def _hamming(a: str, b: str) -> int:
    a, b = a.ljust(max(len(a), len(b))), b.ljust(max(len(a), len(b)))
    return sum(1 for x, y in zip(a, b, strict=False) if x != y)


def _duplicate_count(db: Session, org_id: str, plate: str | None) -> int:
    if not plate:
        return 0
    window = datetime.now(UTC) - timedelta(seconds=settings.alpr_duplicate_window_seconds)
    return int(
        db.scalar(
            select(Capture.id)
            .where(
                and_(
                    Capture.org_id == org_id,
                    Capture.plate == plate,
                    Capture.created_at >= window,
                )
            )
            .limit(1)
        )
        is not None
    )


def ingest(
    db: Session,
    *,
    org: Organization,
    user: User | None,
    data: bytes,
    filename: str = "upload.jpg",
    camera_id: str | None = None,
    country: str | None = None,
    notes: str | None = None,
    source: str = "upload",
    lat: float | None = None,
    lon: float | None = None,
    result: RecognitionResult | None = None,
) -> tuple[Capture, RecognitionResult]:
    validate_upload(data)
    image = imaging.load_image(data)
    array = imaging.to_numpy(image)
    perceptual = imaging.average_hash(array)

    key, size = storage.save(data, org_id=org.id)
    thumb_bytes = imaging.thumbnail(array, max_dim=720)
    thumb_key = storage.write_derived(_thumb_key(key), thumb_bytes)

    options = EngineOptions(
        country=country or settings.alpr_default_country,
        max_candidates=settings.alpr_max_candidates,
    )
    # A caller may supply an already-computed reading. Demo seeding uses this to
    # replay a frozen result instead of re-running OCR over fixtures it has
    # recognised hundreds of times before. Live uploads always take the ``None``
    # path, so this changes nothing for a real scan — and a cache written by an
    # older engine is discarded rather than trusted.
    if result is None:
        result = get_engine().recognize(data, options)
    elif result.engine_version != ENGINE_VERSION:
        logger.warning(
            "Precomputed reading came from %s, engine is %s — re-reading",
            result.engine_version,
            ENGINE_VERSION,
        )
        result = get_engine().recognize(data, options)
    best = result.best

    watchlist_entry = lookup_watchlist(db, org.id, best.plate if best else None)
    duplicates = _duplicate_count(db, org.id, best.plate if best else None)
    hour = datetime.now(UTC).astimezone().hour
    risk, risk_level = grammar.risk_score(
        watchlist_hit=watchlist_entry is not None,
        watchlist_severity=watchlist_entry.severity if watchlist_entry else "low",
        confidence=best.confidence if best else 0.0,
        hour=hour,
        duplicate=bool(duplicates),
    )

    if watchlist_entry is not None:
        status = "flagged"
    elif best and best.confidence >= settings.alpr_review_confidence and best.format_valid:
        status = "verified"
    else:
        status = "pending"

    primary_camera = None
    if camera_id:
        from app.models.entities import Camera

        primary_camera = db.scalars(
            select(Camera).where(Camera.id == camera_id, Camera.org_id == org.id)
        ).first()
        if primary_camera is None:
            raise ValidationFailed("Unknown camera for this workspace.", code="camera_not_found")

    capture = Capture(
        org_id=org.id,
        user_id=user.id if user else None,
        camera_id=primary_camera.id if primary_camera else None,
        plate=best.plate if best else None,
        plate_display=_display_plate(best.plate) if best else None,
        country=best.country if best else "GENERIC",
        region=best.region if best else None,
        plate_type=best.plate_type if best else "unknown",
        confidence=best.confidence if best else 0.0,
        status=status,
        risk_level=risk_level,
        risk_score=risk,
        watchlist_hit=watchlist_entry is not None,
        image_path=key,
        thumbnail_path=thumb_key,
        image_width=result.image_width,
        image_height=result.image_height,
        image_bytes=size,
        source=source,
        processing_ms=result.processing_ms,
        engine_version=result.engine_version,
        candidates=[
            {
                "plate": p.plate,
                "confidence": round(p.confidence, 4),
                "format": p.format_name,
                "box": p.box.as_dict(),
                "glyphs": [
                    {"label": glyph.label, "confidence": round(glyph.confidence, 3)}
                    for glyph in p.glyphs
                ],
            }
            for p in result.plates[:4]
        ],
        alternates=best.alternates if best else [],
        diagnostics={
            **(best.diagnostics if best else {}),
            "format": best.format_name if best else None,
            "format_valid": best.format_valid if best else False,
            "raw_text": best.raw_text if best else None,
            "hash": perceptual,
            "timings": {k: round(v, 2) for k, v in result.timings.items()},
            "quality": result.diagnostics.get("quality"),
            "duplicate_in_window": bool(duplicates),
        },
        notes=notes,
        lat=lat,
        lon=lon,
    )
    db.add(capture)

    if watchlist_entry is not None:
        watchlist_entry.hits += 1
        watchlist_entry.last_hit_at = datetime.now(UTC)
        db.add(watchlist_entry)

    db.commit()
    db.refresh(capture)

    audit.record(
        db,
        org_id=org.id,
        actor=user.email if user else "api",
        action="capture.created",
        entity_type="capture",
        entity_id=capture.id,
        detail={"plate": capture.plate, "status": status, "confidence": round(capture.confidence, 3)},
        commit=True,
    )
    _notify(db, org_id=org.id, capture=capture, watchlist_entry=watchlist_entry)
    logger.info(
        "Capture stored",
        extra={"plate": capture.plate or "-", "camera_id": capture.camera_id},
    )
    return capture, result


def _notify(db: Session, *, org_id: str, capture: Capture, watchlist_entry: WatchlistEntry | None) -> None:
    payload: dict[str, Any] = {
        "capture_id": capture.id,
        "plate": capture.plate,
        "confidence": round(capture.confidence, 4),
        "status": capture.status,
        "risk_level": capture.risk_level,
        "camera_id": capture.camera_id,
        "created_at": capture.created_at.isoformat(),
    }
    try:
        webhooks.dispatch(db, org_id, "capture.created", payload)
        if watchlist_entry is not None:
            webhooks.dispatch(
                db,
                org_id,
                "watchlist.hit",
                {
                    **payload,
                    "category": watchlist_entry.category,
                    "severity": watchlist_entry.severity,
                    "label": watchlist_entry.label,
                },
            )
    except Exception:  # pragma: no cover - never fail ingestion on webhook errors
        logger.warning("Webhook dispatch failed", exc_info=True)


def _display_plate(plate: str | None) -> str | None:
    if not plate:
        return plate
    if len(plate) >= 9 and plate[:2].isalpha():
        return " ".join(filter(None, [plate[:2], plate[2:4], plate[4:-4], plate[-4:]]))
    return plate


def _thumb_key(key: str) -> str:
    if "." in key.rsplit("/", 1)[-1]:
        stem, _, _ext = key.rpartition(".")
        return f"{stem}-thumb.jpg"
    return f"{key}-thumb.jpg"


def reanalyze(db: Session, capture: Capture, org_id: str) -> RecognitionResult:
    """Re-run inference on the stored original (e.g. after a grammar update)."""
    data = storage.read(capture.image_path)
    result = get_engine().recognize(data, EngineOptions(country=capture.country))
    best = result.best
    if best:
        capture.plate = best.plate
        capture.plate_display = best.plate
        capture.country = best.country
        capture.region = best.region
        capture.plate_type = best.plate_type
        capture.confidence = best.confidence
        capture.candidates = [
            {"plate": p.plate, "confidence": round(p.confidence, 4)} for p in result.plates[:4]
        ]
        capture.alternates = best.alternates
        capture.processing_ms = result.processing_ms
        db.add(capture)
        db.commit()
        db.refresh(capture)
    audit.record(
        db,
        org_id=org_id,
        actor="system",
        action="capture.reanalyzed",
        entity_type="capture",
        entity_id=capture.id,
        detail={"plate": capture.plate},
    )
    return result
