"""Demo seeding must stay fast enough to survive a cold boot.

These tests exist because a deploy once died with "no open ports detected":
seeding ran 34 real OCR passes at 17-34 s each on a free-tier CPU, a quarter of
an hour before uvicorn bound anything. The fix is a frozen recognition cache
plus background seeding, and both are easy to undo by accident — so they are
pinned here.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from app.alpr.engine import ENGINE_VERSION  # noqa: E402
from app.services import seed_cache  # noqa: E402

CACHE_PATH = ROOT / "app" / "assets" / "seed" / "recognitions.json"


@pytest.fixture()
def db_session(tmp_path):
    """A throwaway database, so seeding never disturbs the shared test tenant."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db.session import Base
    from app.models import entities  # noqa: F401  (register metadata)

    engine = create_engine(f"sqlite:///{tmp_path / 'seed.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture(scope="module")
def cache() -> dict:
    return json.loads(CACHE_PATH.read_text())


def test_cache_is_shipped(cache):
    """The cache has to be in the image, or every boot falls back to live OCR."""
    assert CACHE_PATH.exists(), "run scripts/build_seed_cache.py"
    assert cache["frames"] == 34
    assert len(cache["recognitions"]) == 34


def test_cache_matches_the_current_engine(cache):
    """A stale cache means the demo shows readings the engine would not produce.

    Regenerate with ``python scripts/build_seed_cache.py`` after changing the
    engine — that is the intended forcing function, not a false alarm.
    """
    assert cache["engine_version"] == ENGINE_VERSION


def test_every_fixture_has_a_frozen_reading(cache):
    from build_seed_cache import seed_frames

    for frame in seed_frames():
        entry = cache["recognitions"].get(str(frame["index"]))
        assert entry is not None, f"frame {frame['index']} missing from the cache"
        assert entry["plates"], f"frame {frame['index']} froze no reading"


def test_frozen_reading_round_trips(cache):
    """Serialise/deserialise must preserve what the UI displays."""
    entry = cache["recognitions"]["0"]
    restored = seed_cache.deserialize(entry)
    best = restored.best
    assert best is not None
    assert best.plate == entry["plates"][0]["plate"]
    assert best.format_valid == entry["plates"][0]["format_valid"]
    assert len(best.glyphs) == len(entry["plates"][0]["glyphs"])
    assert restored.engine_version == ENGINE_VERSION


def test_seeding_never_calls_the_engine(db_session, monkeypatch):
    """The whole point: a demo boot must not pay for OCR.

    If a future change drops the cached result and lets ``ingest`` fall through
    to the live engine, this test fails in a second instead of the deploy
    failing in fifteen minutes.
    """
    from app.seed import seed_if_empty
    from app.services import recognition

    def _explode():
        raise AssertionError("demo seeding ran live OCR — the frozen cache was bypassed")

    # Patch the name as *recognition* sees it: the module imported ``get_engine``
    # directly, so patching ``app.alpr.engine.get_engine`` would silently do
    # nothing and leave this test passing for the wrong reason.
    monkeypatch.setattr(recognition, "get_engine", _explode)

    started = time.perf_counter()
    created = seed_if_empty(db_session)
    elapsed = time.perf_counter() - started

    assert created is True
    # Seeding tolerates a bad frame by skipping it, so "it returned True" is not
    # evidence that OCR was avoided — every frame could have exploded. The demo
    # has to actually contain its 34 captures.
    from sqlalchemy import func, select

    from app.models.entities import Capture

    assert db_session.scalar(select(func.count(Capture.id))) == 34
    # Generous bound: a cold CI runner renders 34 frames without a warm cache.
    # Live OCR at 17-34 s a frame would take fifteen minutes.
    assert elapsed < 90, f"seeding took {elapsed:.1f}s — it is re-reading fixtures live"


def test_seeded_workspace_keeps_real_readings(db_session):
    """Speed must not cost authenticity: confidences and statuses are genuine."""
    from sqlalchemy import func, select

    from app.models.entities import Capture
    from app.seed import seed_if_empty

    seed_if_empty(db_session)
    total = db_session.scalar(select(func.count(Capture.id)))
    assert total == 34

    captures = db_session.scalars(select(Capture)).all()
    assert all(c.engine_version == ENGINE_VERSION for c in captures)
    # Real pipeline output, not fabricated: confidences vary and some are weak.
    confidences = {round(c.confidence, 2) for c in captures}
    assert len(confidences) > 5
    assert any(c.confidence < 0.9 for c in captures)
    assert {c.status for c in captures} >= {"verified"}
    assert all(c.plate for c in captures)
