"""Idempotent demo-workspace provisioning.

Everything here is produced by the *real* pipeline over synthetic frames, so the
demo shows genuine detections, confidence scores and timings rather than
fabricated JSON.

Those readings are frozen at build time in ``app/assets/seed/recognitions.json``
(see ``scripts/build_seed_cache.py``) instead of being recomputed on boot. A free
tier CPU needs 17-34 s per frame, so re-reading 34 fixtures cost a quarter of an
hour on every cold start — before the port was even bound, which is what made
deploys time out. Replaying the frozen reading keeps the demo authentic and makes
seeding a couple of seconds of image rendering.
"""
from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alpr.types import RecognitionResult
from app.core.config import PROJECT_ROOT, settings
from app.core.logging import get_logger
from app.core.security import hash_password
from app.models.entities import (
    Camera,
    Capture,
    Organization,
    User,
    WatchlistEntry,
)
from app.services import recognition, samples, seed_cache

logger = get_logger(__name__)

ORG_NAME = "Meridian Traffic Authority"
ORG_SLUG = "meridian"

CAMERAS = [
    ("NH16-TOLL-01", "NH-16 Toll Plaza", "NH-16 · Brahmapur", "NH-16 Coastal", 4, 30.0),
    ("RING-EAST-07", "Ring Road East", "Ring Road · Junction 7", "Ring Road East", 3, 25.0),
    ("HARBOUR-LINK-2", "Harbour Link", "Harbour Link · Gate 2", "Harbour Link", 2, 25.0),
    ("AIRPORT-EXP-3", "Airport Expressway", "Expressway · Km 12", "Airport Expressway", 3, 30.0),
    ("OLDTOWN-GATE", "Old Town Gate", "Old Town · Heritage Gate", "Old Town Gate", 1, 15.0),
    ("RING-WEST-11", "Ring Road West", "Ring Road · Junction 11", "Ring Road West", 2, 25.0),
]

USERS = [
    ("elara@platevision.io", "Elara Voss", "admin", "ES", "North Corridor", settings.demo_password),
    ("marcus@platevision.io", "Marcus Odell", "supervisor", "MO", "Central Command", "nightwatch"),
    ("priya@platevision.io", "Priya Raman", "operator", "PR", "Coastal Zone", "nightwatch"),
    ("noah@platevision.io", "Noah Kimura", "operator", "NK", "Airport Zone", "nightwatch"),
    ("ada@platevision.io", "Ada Ferreira", "viewer", "AF", "Audit", "nightwatch"),
]

WATCHLIST = [
    ("DL08CD9021", "Stolen · Honda City", "stolen", "critical", "Reported stolen FIR 4412/2024"),
    ("GJ01WT8090", "Permit lapsed · Tanker", "permit", "medium", "Commercial permit expired 12 days ago"),
    ("AP39TR7712", "Banned operator", "banned", "high", "Repeat overloading violations"),
    ("MH12AB3456", "VIP convoy", "vip", "low", "Escort clearance — do not intercept"),
    ("HR26DK8337", "Toll evasion", "unpaid", "medium", "₹18,400 outstanding across 11 crossings"),
    ("KA05MJ4412", "Insurance lapsed", "other", "low", "Policy expired — advisory only"),
]


SEED_CACHE_PATH = PROJECT_ROOT / "app" / "assets" / "seed" / "recognitions.json"


@lru_cache(maxsize=1)
def _load_recognition_cache() -> dict[str, dict]:
    """Frozen readings for the demo fixtures, keyed by fixture index.

    Missing or corrupt cache degrades to the live pipeline rather than failing a
    boot: a slow demo is much better than no demo.
    """
    try:
        payload = json.loads(SEED_CACHE_PATH.read_text())
    except FileNotFoundError:
        logger.warning("Seed recognition cache missing; demo frames will be re-read live")
        return {}
    except Exception:
        logger.warning("Seed recognition cache unreadable; demo frames will be re-read live",
                       exc_info=True)
        return {}
    return payload.get("recognitions", {})


def _cached_reading(index: int) -> RecognitionResult | None:
    raw = _load_recognition_cache().get(str(index))
    if raw is None:
        return None
    try:
        return seed_cache.deserialize(raw)
    except Exception:  # pragma: no cover - defensive against a hand-edited cache
        logger.warning("Could not rebuild cached reading for frame %s", index, exc_info=True)
        return None


def seed_if_empty(db: Session) -> bool:
    if db.scalar(select(func.count(Organization.id))):
        return False
    logger.info("Provisioning demo workspace …")
    org = Organization(
        name=ORG_NAME,
        slug=ORG_SLUG,
        plan="enterprise",
        monthly_scan_quota=250_000,
        brand_accent="#C9B8FF",
    )
    db.add(org)
    db.flush()

    users: list[User] = []
    for email, name, role, initials, zone, password in USERS:
        user = User(
            org_id=org.id,
            email=email,
            full_name=name,
            role=role,
            avatar_initials=initials,
            zone=zone,
            password_hash=hash_password(password),
        )
        db.add(user)
        users.append(user)
    db.flush()

    cameras: list[Camera] = []
    for index, (code, name, location, corridor, lanes, fps) in enumerate(CAMERAS):
        camera = Camera(
            org_id=org.id,
            code=code,
            name=name,
            location=location,
            corridor=corridor,
            lanes=lanes,
            fps=fps,
            status="online" if index < 5 else "degraded",
            firmware=f"pv-eye 2.{2 + index % 3}.1",
            last_seen_at=datetime.now(UTC) - timedelta(minutes=index * 3),
        )
        db.add(camera)
        cameras.append(camera)
    db.flush()

    for plate, label, category, severity, reason in WATCHLIST:
        db.add(
            WatchlistEntry(
                org_id=org.id,
                plate=plate,
                label=label,
                category=category,
                severity=severity,
                reason=reason,
                created_by="Elara Voss",
            )
        )
    db.commit()

    admin = users[0]
    _seed_captures(db, org, users, cameras, admin)
    logger.info("Demo workspace ready: %d users, %d cameras", len(users), len(cameras))
    return True


def _seed_captures(
    db: Session,
    org: Organization,
    users: list[User],
    cameras: list[Camera],
    admin: User,
) -> None:
    rng = random.Random(20241024)
    now = datetime.now(UTC)
    plan: list[dict] = []
    for index in range(34):
        text, country, kind = samples.PLATE_POOL[index % len(samples.PLATE_POOL)]
        scene = list(samples.SCENES)[index % len(samples.SCENES)]
        days_ago = rng.choice([0, 0, 0, 1, 1, 2, 2, 3, 4, 5, 6, 8, 9, 11, 13])
        created = now - timedelta(days=days_ago, hours=rng.uniform(0, 20), minutes=rng.uniform(0, 59))
        plan.append(
            {
                "text": text,
                "country": country,
                "kind": kind,
                "scene": scene,
                "created_at": created,
                "camera": cameras[index % len(cameras)],
                "user": users[index % len(users)],
                "index": index,
            }
        )

    created_captures: list[tuple[Capture, dict]] = []
    for item in plan:
        image = samples.render_scene(
            item["text"],
            kind=item["kind"],
            scene=item["scene"],
            seed=item["index"] * 7 + 3,
        )
        try:
            capture, _ = recognition.ingest(
                db,
                org=org,
                user=item["user"],
                data=image,
                filename=f"frame-{item['index']:03d}.jpg",
                camera_id=item["camera"].id,
                country="IN",
                source="stream",
                lat=19.31 + rng.uniform(-0.12, 0.12),
                lon=84.79 + rng.uniform(-0.12, 0.12),
                result=_cached_reading(item["index"]),
            )
        except Exception:  # pragma: no cover - a single bad frame must not abort seeding
            logger.warning("Seed frame %s failed", item["index"], exc_info=True)
            continue
        created_captures.append((capture, item))

    # Backdate captures and diversify review states.
    for capture, item in created_captures:
        capture.created_at = item["created_at"]
        capture.updated_at = item["created_at"]
        if not capture.watchlist_hit:
            roll = rng.random()
            if roll > 0.86:
                capture.status = "flagged"
                capture.risk_level = "high"
                capture.risk_score = round(rng.uniform(58, 78), 1)
            elif roll > 0.74:
                capture.status = "pending"
            elif roll > 0.70:
                capture.status = "denied"
            else:
                capture.status = "verified"
                if capture.confidence > 0.9 and rng.random() > 0.6:
                    capture.reviewed_by = rng.choice(users).full_name
                    capture.reviewed_at = item["created_at"] + timedelta(minutes=2)
        db.add(capture)
    db.commit()

    # A few operator notes make the detail drawer feel lived-in.
    noted = db.scalars(
        select(Capture).where(Capture.org_id == org.id).order_by(Capture.created_at.desc()).limit(4)
    ).all()
    notes = [
        "Verified against FASTag ledger — clearance issued.",
        "Frame quality low at dusk; operator confirmed by eye.",
        "Escalated to control room; patrol dispatched.",
        "Duplicate read of an earlier crossing — merged.",
    ]
    for capture, note in zip(noted, notes, strict=False):
        capture.notes = note
        db.add(capture)
    db.commit()

    from app.services import audit

    audit.record(
        db,
        org_id=org.id,
        actor=admin.email,
        action="workspace.provisioned",
        entity_type="organization",
        entity_id=org.id,
        detail={"cameras": len(cameras), "watchlist": len(WATCHLIST)},
    )


if __name__ == "__main__":  # pragma: no cover
    from app.db.session import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        created = seed_if_empty(session)
    print("Seeded." if created else "Workspace already exists — nothing to do.")
