"""ORM -> API payload mapping."""
from __future__ import annotations

from collections.abc import Iterable

from app.alpr import grammar
from app.models.entities import Camera, Capture, WatchlistEntry


def capture_out(
    capture: Capture,
    *,
    camera: Camera | None = None,
    watchlist: WatchlistEntry | None = None,
) -> dict:
    camera = camera or capture.camera
    display = capture.plate_display or _format_plate(capture.plate)
    return {
        "id": capture.id,
        "plate": capture.plate,
        "plate_display": display,
        "country": capture.country,
        "region": capture.region,
        "region_name": grammar.describe_region(capture.region, capture.plate),
        "plate_type": capture.plate_type,
        "confidence": round(capture.confidence, 4),
        "status": capture.status,
        "risk_level": capture.risk_level,
        "risk_score": round(capture.risk_score, 1),
        "watchlist_hit": capture.watchlist_hit,
        "watchlist_label": watchlist.label if watchlist else None,
        "image_url": f"/api/v1/captures/{capture.id}/image",
        "thumbnail_url": f"/api/v1/captures/{capture.id}/image?variant=thumb",
        "image": {"width": capture.image_width, "height": capture.image_height},
        "camera_id": capture.camera_id,
        "camera_name": camera.name if camera else None,
        "corridor": camera.corridor if camera else None,
        "operator": capture.user.full_name if capture.user else "Automated ingest",
        "source": capture.source,
        "processing_ms": round(capture.processing_ms, 2),
        "engine_version": capture.engine_version,
        "notes": capture.notes,
        "candidates": capture.candidates or [],
        "alternates": capture.alternates or [],
        "diagnostics": capture.diagnostics or {},
        "lat": capture.lat,
        "lon": capture.lon,
        "reviewed_by": capture.reviewed_by,
        "reviewed_at": capture.reviewed_at,
        "created_at": capture.created_at,
        "updated_at": capture.updated_at,
    }


def _format_plate(plate: str | None) -> str:
    if not plate:
        return "UNREADABLE"
    if len(plate) >= 9 and plate[:2].isalpha():
        return f"{plate[:2]} {plate[2:4]} {plate[4:-4]} {plate[-4:]}".replace("  ", " ").strip()
    return plate


def watchlist_out(entry: WatchlistEntry) -> dict:
    return {
        "id": entry.id,
        "plate": entry.plate,
        "label": entry.label,
        "category": entry.category,
        "severity": entry.severity,
        "reason": entry.reason,
        "active": entry.active,
        "hits": entry.hits,
        "last_hit_at": entry.last_hit_at,
        "expires_at": entry.expires_at,
        "created_at": entry.created_at,
    }


def watchlist_map(entries: Iterable[WatchlistEntry]) -> dict[str, WatchlistEntry]:
    return {e.plate: e for e in entries}
