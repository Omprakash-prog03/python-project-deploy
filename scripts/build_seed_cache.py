"""Generate the frozen recognition cache used by demo seeding.

Seeding the demo workspace replays the real pipeline over 34 synthetic frames.
That is the right thing to do — the demo shows genuine detections, confidences
and timings rather than fabricated JSON — but it costs 17-34 s *per frame* on a
free-tier CPU, so a cold boot spent fifteen minutes inside OCR before the port
was ever bound. Render kills a deploy that does not bind in time, and its free
tier wipes the disk on spin-down, so the cost was paid on every wake-up.

The frames themselves are deterministic, so their readings are too. This script
runs the pipeline once and freezes the result. Seeding then replays the frozen
reading and only re-renders the image, which is a PIL call of a few milliseconds.

Live scans are untouched: this cache is only ever consulted for the synthetic
demo fixtures.

    python scripts/build_seed_cache.py

Re-run it whenever the demo fixtures or the engine change. ``tests/test_seed.py``
fails if the cache is stale, so CI enforces that.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.alpr.engine import ENGINE_VERSION, EngineOptions, get_engine
from app.services import samples, seed_cache

CACHE_PATH = (
    Path(__file__).resolve().parent.parent / "app" / "assets" / "seed" / "recognitions.json"
)


def seed_frames() -> list[dict]:
    """The exact fixture list ``app.seed._seed_captures`` replays.

    Kept here as the single source of truth so the cache and the seeding code
    can never drift apart.
    """
    frames: list[dict] = []
    for index in range(34):
        text, country, kind = samples.PLATE_POOL[index % len(samples.PLATE_POOL)]
        scene = list(samples.SCENES)[index % len(samples.SCENES)]
        frames.append(
            {
                "index": index,
                "text": text,
                "country": "IN",
                "kind": kind,
                "scene": scene,
                "seed": index * 7 + 3,
            }
        )
    return frames


def build() -> dict:
    engine = get_engine()
    entries: dict[str, dict] = {}
    frames = seed_frames()
    for position, frame in enumerate(frames, start=1):
        payload = samples.render_scene(
            frame["text"],
            kind=frame["kind"],
            scene=frame["scene"],
            seed=frame["seed"],
        )
        result = engine.recognize(payload, EngineOptions(country=frame["country"]))
        entries[str(frame["index"])] = seed_cache.serialize(result)
        best = result.best
        print(
            f"  {position:>2}/{len(frames)}  {frame['text']}  ->  "
            f"{best.plate if best else '-':<12} {result.processing_ms:>8.0f} ms",
            file=sys.stderr,
        )
    return {
        "engine_version": ENGINE_VERSION,
        "frames": len(frames),
        "fixtures": frames,
        "recognitions": entries,
    }


def main() -> int:
    payload = build()
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(payload, separators=(",", ":")))
    size_kb = CACHE_PATH.stat().st_size / 1024
    print(f"\nwrote {CACHE_PATH} ({size_kb:.1f} KB, {payload['frames']} frames)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
