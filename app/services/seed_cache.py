"""Codec for the frozen demo-seed recognition cache.

Seeding the demo workspace replays the real pipeline over 34 deterministic
synthetic frames. Running that pipeline on boot costs 17-34 s *per frame* on a
free-tier CPU, which is a quarter of an hour before the port is ever bound —
long enough that a deploy is killed as "no open ports detected", and it is paid
again on every wake-up because the free tier wipes its disk.

The frames are deterministic, so their readings are too. ``serialize`` freezes
one and ``deserialize`` rebuilds it, letting seeding re-render the image (a PIL
call of a few milliseconds) and attach the reading it has always produced.

Only demo fixtures ever consult this cache. Live uploads always run the engine.
"""
from __future__ import annotations

from typing import Any

from app.alpr.types import Box, Glyph, PlateReading, RecognitionResult


def serialize(result: RecognitionResult) -> dict[str, Any]:
    """Freeze a recognition result as JSON-safe data."""
    return {
        "image_width": result.image_width,
        "image_height": result.image_height,
        "processing_ms": round(result.processing_ms, 2),
        "engine_version": result.engine_version,
        "timings": {k: round(v, 2) for k, v in result.timings.items()},
        "diagnostics": result.diagnostics,
        "plates": [
            {
                "plate": p.plate,
                "raw_text": p.raw_text,
                "confidence": round(p.confidence, 4),
                "box": p.box.as_dict(),
                "quad": p.quad,
                "country": p.country,
                "region": p.region,
                "plate_type": p.plate_type,
                "format_name": p.format_name,
                "format_valid": p.format_valid,
                "alternates": p.alternates,
                "diagnostics": p.diagnostics,
                # Glyph bitmaps are deliberately dropped: nothing downstream of
                # ``recognize`` reads them, and they would bloat the cache from
                # kilobytes to megabytes.
                "glyphs": [
                    {
                        "label": g.label,
                        "confidence": round(g.confidence, 4),
                        "box": g.box.as_dict(),
                    }
                    for g in p.glyphs
                ],
            }
            for p in result.plates
        ],
    }


def deserialize(entry: dict[str, Any]) -> RecognitionResult:
    """Rebuild a :class:`RecognitionResult` from a frozen entry."""
    plates: list[PlateReading] = []
    for raw in entry["plates"]:
        box = raw["box"]
        plates.append(
            PlateReading(
                plate=raw["plate"],
                raw_text=raw["raw_text"],
                confidence=float(raw["confidence"]),
                box=Box(box["x"], box["y"], box["w"], box["h"]),
                quad=raw.get("quad"),
                country=raw["country"],
                region=raw.get("region"),
                plate_type=raw.get("plate_type", "private"),
                format_name=raw.get("format_name"),
                format_valid=bool(raw.get("format_valid")),
                alternates=raw.get("alternates", []),
                diagnostics=raw.get("diagnostics", {}),
                glyphs=[
                    Glyph(
                        bitmap=None,
                        box=Box(g["box"]["x"], g["box"]["y"], g["box"]["w"], g["box"]["h"]),
                        label=g["label"],
                        confidence=float(g["confidence"]),
                    )
                    for g in raw.get("glyphs", [])
                ],
            )
        )
    return RecognitionResult(
        plates=plates,
        image_width=int(entry["image_width"]),
        image_height=int(entry["image_height"]),
        processing_ms=float(entry["processing_ms"]),
        engine_version=entry["engine_version"],
        timings=entry.get("timings", {}),
        diagnostics=entry.get("diagnostics", {}),
    )
