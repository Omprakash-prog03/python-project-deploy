"""Operational analytics for the command dashboard."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.entities import Camera, Capture, Organization, WatchlistEntry
from app.services.serializers import capture_out, watchlist_out

RANGE_MAP = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}


def _range_start(range_key: str) -> datetime:
    days = RANGE_MAP.get(range_key, 7)
    return datetime.now(UTC) - timedelta(days=days)


def overview(db: Session, org: Organization, range_key: str = "7d", tz_offset_minutes: int = 0) -> dict:
    range_key = range_key if range_key in RANGE_MAP else "7d"
    days = RANGE_MAP[range_key]
    start = _range_start(range_key)
    previous_start = start - timedelta(days=days)

    captures = db.scalars(
        select(Capture)
        .options(selectinload(Capture.camera), selectinload(Capture.user))
        .where(Capture.org_id == org.id, Capture.created_at >= start)
        .order_by(Capture.created_at.desc())
    ).all()

    previous_counts = dict(
        db.execute(
            select(Capture.status, func.count(Capture.id))
            .where(
                Capture.org_id == org.id,
                Capture.created_at >= previous_start,
                Capture.created_at < start,
            )
            .group_by(Capture.status)
        ).all()
    )
    previous_total = sum(previous_counts.values())

    total = len(captures)
    unique_plates = len({c.plate for c in captures if c.plate})
    flagged = sum(1 for c in captures if c.status == "flagged")
    watchlist_hits = sum(1 for c in captures if c.watchlist_hit)
    manual = sum(1 for c in captures if c.status == "pending")
    confidences = [c.confidence for c in captures if c.confidence > 0]
    processing = [c.processing_ms for c in captures if c.processing_ms > 0]
    verified = sum(1 for c in captures if c.status in ("verified", "flagged"))

    trend_scans = _pct_change(total, previous_total)
    accuracy = round(100 * verified / total, 2) if total else 0.0

    buckets = _buckets(range_key, tz_offset_minutes)
    series_map: dict[str, dict] = {
        label: {"scans": 0, "flagged": 0, "confidence_sum": 0.0, "confidence_n": 0}
        for label in buckets
    }
    for capture in captures:
        label = _label_for(capture.created_at, range_key, tz_offset_minutes)
        if label not in series_map:
            continue
        slot = series_map[label]
        slot["scans"] += 1
        if capture.status == "flagged" or capture.watchlist_hit:
            slot["flagged"] += 1
        if capture.confidence > 0:
            slot["confidence_sum"] += capture.confidence
            slot["confidence_n"] += 1

    series = [
        {
            "label": label,
            "scans": slot["scans"],
            "flagged": slot["flagged"],
            "avg_confidence": round(
                slot["confidence_sum"] / slot["confidence_n"] * 100, 1
            ) if slot["confidence_n"] else 0.0,
        }
        for label, slot in series_map.items()
    ]

    corridors = Counter((c.camera.corridor or "Unassigned") for c in captures if c.camera)
    plate_types = Counter(c.plate_type or "unknown" for c in captures)
    risk_mix = Counter(c.risk_level for c in captures)
    status_mix = Counter(c.status for c in captures)

    cameras = db.scalars(select(Camera).where(Camera.org_id == org.id)).all()
    watchlist_entries = db.scalars(
        select(WatchlistEntry)
        .where(WatchlistEntry.org_id == org.id, WatchlistEntry.active.is_(True))
        .order_by(WatchlistEntry.created_at.desc())
        .limit(6)
    ).all()

    recent = captures[:8]
    watchlist_by_plate = {
        e.plate: e
        for e in db.scalars(
            select(WatchlistEntry).where(WatchlistEntry.org_id == org.id)
        ).all()
    }

    return {
        "range": range_key,
        "kpis": {
            "scans": total,
            "unique_plates": unique_plates,
            "flagged": flagged,
            "watchlist_hits": watchlist_hits,
            "accuracy": accuracy,
            "avg_confidence": round(
                (sum(confidences) / len(confidences) * 100) if confidences else 0.0, 1
            ),
            "avg_processing_ms": round(sum(processing) / len(processing), 1) if processing else 0.0,
            "manual_review": manual,
            "trend": {
                "scans": trend_scans,
                "flagged": _pct_change(flagged, previous_counts.get("flagged", 0)),
                "accuracy": round(accuracy - _previous_accuracy(previous_counts, previous_total), 2),
            },
        },
        "series": series,
        "corridors": _breakdown(corridors, total),
        "plate_types": _breakdown(plate_types, total),
        "risk_mix": _breakdown(risk_mix, total),
        "status_mix": _breakdown(status_mix, total),
        "recent": [
            capture_out(c, watchlist=watchlist_by_plate.get(c.plate or "")) for c in recent
        ],
        "watchlist": [watchlist_out(e) for e in watchlist_entries],
        "cameras_online": sum(1 for c in cameras if c.status == "online"),
        "cameras_total": len(cameras),
    }


def _previous_accuracy(previous_counts: dict, previous_total: int) -> float:
    if not previous_total:
        return 0.0
    verified = previous_counts.get("verified", 0) + previous_counts.get("flagged", 0)
    return round(100 * verified / previous_total, 2)


def _pct_change(current: int, previous: int) -> float:
    if previous == 0:
        return 100.0 if current > 0 else 0.0
    return round((current - previous) / previous * 100, 1)


def _breakdown(counter: Counter, total: int) -> list[dict]:
    items = counter.most_common()
    if not items:
        return []
    return [
        {
            "label": label.replace("_", " ").title(),
            "value": value,
            "share": round(value / total * 100, 1) if total else 0.0,
        }
        for label, value in items
    ]


def _buckets(range_key: str, tz_offset: int) -> list[str]:
    now = datetime.now(UTC) + timedelta(minutes=tz_offset)
    if range_key == "24h":
        return [f"{(now - timedelta(hours=h)).hour:02d}:00" for h in range(23, -1, -1)]
    days = RANGE_MAP.get(range_key, 7)
    return [
        (now - timedelta(days=d)).strftime("%d %b") for d in range(days - 1, -1, -1)
    ]


def _label_for(created_at: datetime, range_key: str, tz_offset: int) -> str:
    stamp = created_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    stamp = stamp.astimezone(UTC) + timedelta(minutes=tz_offset)
    if range_key == "24h":
        return f"{stamp.hour:02d}:00"
    return stamp.strftime("%d %b")


def heatmap(db: Session, org: Organization, days: int = 7) -> list[dict]:
    """Day-of-week x hour activity grid."""
    start = datetime.now(UTC) - timedelta(days=days)
    rows = db.execute(
        select(Capture.created_at).where(Capture.org_id == org.id, Capture.created_at >= start)
    ).all()
    grid = defaultdict(int)
    for (created_at,) in rows:
        stamp = created_at
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        grid[(stamp.weekday(), stamp.hour)] += 1
    return [
        {"day": day, "hour": hour, "scans": grid.get((day, hour), 0)}
        for day in range(7)
        for hour in range(24)
    ]
