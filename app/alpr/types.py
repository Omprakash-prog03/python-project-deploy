"""Typed result objects for the recognition pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def aspect(self) -> float:
        return self.w / self.h if self.h else 0.0

    def as_dict(self) -> dict[str, int]:
        return {"x": int(self.x), "y": int(self.y), "w": int(self.w), "h": int(self.h)}

    def clip(self, width: int, height: int) -> Box:
        x = max(0, min(self.x, width - 1))
        y = max(0, min(self.y, height - 1))
        w = max(1, min(self.w, width - x))
        h = max(1, min(self.h, height - y))
        return Box(x, y, w, h)

    def pad(self, ratio: float, width: int, height: int) -> Box:
        dx = int(self.w * ratio)
        dy = int(self.h * ratio)
        return Box(self.x - dx, self.y - dy, self.w + 2 * dx, self.h + 2 * dy).clip(width, height)


@dataclass(slots=True)
class Candidate:
    box: Box
    score: float = 0.0
    strategy: str = "morph"
    quad: list[list[float]] | None = None


@dataclass(slots=True)
class Glyph:
    bitmap: Any  # np.ndarray, uint8, normalized 32x48
    box: Box
    label: str = "?"
    confidence: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)
    features: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class PlateReading:
    plate: str
    raw_text: str
    confidence: float
    box: Box
    quad: list[list[float]] | None = None
    country: str = "GENERIC"
    region: str | None = None
    plate_type: str = "private"
    format_name: str | None = None
    format_valid: bool = False
    glyphs: list[Glyph] = field(default_factory=list)
    alternates: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plate": self.plate,
            "raw_text": self.raw_text,
            "confidence": round(self.confidence, 4),
            "box": self.box.as_dict(),
            "quad": self.quad,
            "country": self.country,
            "region": self.region,
            "plate_type": self.plate_type,
            "format": self.format_name,
            "format_valid": self.format_valid,
            "alternates": self.alternates,
            "diagnostics": self.diagnostics,
        }


@dataclass(slots=True)
class RecognitionResult:
    plates: list[PlateReading]
    image_width: int
    image_height: int
    processing_ms: float
    engine_version: str
    timings: dict[str, float] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def best(self) -> PlateReading | None:
        return self.plates[0] if self.plates else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "plates": [p.as_dict() for p in self.plates],
            "best": self.best.as_dict() if self.best else None,
            "image": {"width": self.image_width, "height": self.image_height},
            "processing_ms": round(self.processing_ms, 2),
            "timings": {k: round(v, 2) for k, v in self.timings.items()},
            "engine_version": self.engine_version,
            "diagnostics": self.diagnostics,
        }
