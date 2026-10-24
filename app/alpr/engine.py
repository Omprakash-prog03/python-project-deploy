"""End-to-end ALPR engine: decode -> detect -> rectify -> segment -> classify -> decode grammar."""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from app.alpr import detect, grammar, imaging, ocr
from app.alpr.types import Box, Glyph, PlateReading, RecognitionResult
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)
ENGINE_VERSION = "pv-alpr/1.2"


@dataclass(slots=True)
class EngineOptions:
    country: str | None = None
    max_candidates: int = 6
    min_confidence: float = 0.0
    enhance: bool = True


class ALPREngine:
    """Stateless, thread-safe recognition engine."""

    def __init__(self, classifier: ocr.GlyphClassifier | None = None) -> None:
        self.classifier = classifier or ocr.get_classifier()
        self.version = ENGINE_VERSION

    # ------------------------------------------------------------------ api
    def recognize(self, image_bytes: bytes, options: EngineOptions | None = None) -> RecognitionResult:
        opts = options or EngineOptions()
        started = time.perf_counter()
        timings: dict[str, float] = {}

        t0 = time.perf_counter()
        image = imaging.load_image(image_bytes)
        array = imaging.to_numpy(image)
        timings["decode_ms"] = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        work, scale = imaging.resize_max(array, settings.alpr_max_image_dim)
        gray = imaging.to_gray(work)
        gray_eq = imaging.clahe(gray, clip=2.2) if opts.enhance else gray
        timings["preprocess_ms"] = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        candidates, proposal_counts = detect.detect(
            work, gray_eq, max_candidates=max(3, min(opts.max_candidates, settings.alpr_max_candidates))
        )
        timings["detect_ms"] = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        plates: list[PlateReading] = []
        for candidate in candidates:
            reading = self._read_candidate(work, gray_eq, candidate, opts, scale)
            if reading is None:
                continue
            if reading.confidence < max(opts.min_confidence, 0.05):
                continue
            if any(_same_plate(reading, other) for other in plates):
                continue
            plates.append(reading)
            if len(plates) >= 3:
                break
        timings["read_ms"] = (time.perf_counter() - t0) * 1000

        plates = _dedupe(plates)
        plates.sort(
            key=lambda p: (
                1.0 if p.format_valid else 0.0,
                p.confidence,
                len(p.plate),
            ),
            reverse=True,
        )
        elapsed = (time.perf_counter() - started) * 1000

        height, width = array.shape[:2]
        result = RecognitionResult(
            plates=plates,
            image_width=width,
            image_height=height,
            processing_ms=elapsed,
            engine_version=self.version,
            timings=timings,
            diagnostics={
                "proposals": proposal_counts,
                "candidates": len(candidates),
                "accepted": len(plates),
                "scale": round(scale, 4),
                "quality": round(imaging.sharpness(gray), 3),
            },
        )
        logger.info(
            "Recognised %d plate(s) in %.1f ms",
            len(plates), elapsed,
            extra={"plate": plates[0].plate if plates else "-"},
        )
        return result

    # -------------------------------------------------------------- internals
    def _read_candidate(
        self,
        work: np.ndarray,
        gray: np.ndarray,
        candidate: detect.Candidate,
        opts: EngineOptions,
        scale: float,
    ) -> PlateReading | None:
        box = candidate.box
        crop = gray[max(0, box.y) : box.y2, max(0, box.x) : box.x2]
        if crop.size == 0 or crop.shape[0] < 12:
            return None

        aspect = max(1.4, min(7.5, box.w / max(1, box.h)))
        best: PlateReading | None = None
        for variant in self._rectify_variants(gray, crop, candidate, aspect):
            reading = self._read_rectified(variant[0], variant[1], box, candidate, opts, scale)
            if reading is None:
                continue
            if best is None or _rank_key(reading) > _rank_key(best):
                best = reading
            # A confident, format-valid read short-circuits the remaining work.
            if best.format_valid and best.confidence >= 0.82:
                break
        return best

    @staticmethod
    def _rectify_variants(
        gray: np.ndarray,
        crop: np.ndarray,
        candidate: detect.Candidate,
        aspect: float,
    ):
        """Yield (rectified_image, target_height) hypotheses, best first."""
        heights = [128, 96]
        for target_h in heights:
            target_w = int(min(760, max(72, target_h * aspect)))
            yield imaging.resize(crop, (target_w, target_h)), target_h
            if candidate.quad and len(candidate.quad) == 4:
                warped = imaging.warp_perspective(gray, candidate.quad, (target_w, target_h))
                if warped.size:
                    yield warped, target_h

    def _read_rectified(
        self,
        rectified: np.ndarray,
        target_h: int,
        box: Box,
        candidate: detect.Candidate,
        opts: EngineOptions,
        scale: float,
    ) -> PlateReading | None:
        target_w = rectified.shape[1]
        if target_w < 40:
            return None
        glyphs, seg_diag = ocr.segment_glyphs(rectified)
        if len(glyphs) < 4:
            return None

        probabilities: list[dict[str, float]] = []
        for glyph in glyphs:
            probs = self.classifier.classify(glyph.bitmap)
            glyph.probabilities = probs
            glyph.label = max(probs, key=probs.get)
            glyph.confidence = float(probs[glyph.label])
            probabilities.append(probs)

        country_hint = (opts.country or settings.alpr_default_country).upper()
        decoded = grammar.decode(probabilities, country=country_hint)
        if decoded is None:
            return None

        top1 = float(np.mean([sorted(p.values(), reverse=True)[0] for p in probabilities]))
        glyph_conf = _glyph_confidence(decoded.probabilities, top1)
        quality = imaging.sharpness(rectified)
        format_bonus = 1.0 if decoded.valid else 0.62
        confidence = (
            0.46 * glyph_conf
            + 0.26 * format_bonus
            + 0.18 * min(1.0, candidate.score)
            + 0.10 * quality
        )
        confidence = float(np.clip(confidence, 0.01, 0.995))
        if not decoded.valid:
            confidence *= 0.78

        country = decoded.format.country
        region = grammar.infer_region(decoded.text, country)
        plate_type = grammar.infer_plate_type(decoded.text, decoded.format)

        inv = 1.0 / (scale if scale > 0 else 1.0)
        mapped_glyphs: list[Glyph] = []
        for glyph in glyphs:
            gx = box.x + (glyph.box.x / target_w) * box.w
            gy = box.y + (glyph.box.y / target_h) * box.h
            gw = (glyph.box.w / target_w) * box.w
            gh = (glyph.box.h / target_h) * box.h
            mapped_glyphs.append(
                Glyph(
                    bitmap=glyph.bitmap,
                    box=Box(int(gx * inv), int(gy * inv), max(1, int(gw * inv)),
                            max(1, int(gh * inv))),
                    label=glyph.label,
                    confidence=round(glyph.confidence, 4),
                    probabilities=glyph.probabilities,
                    features=glyph.features,
                )
            )

        return PlateReading(
            plate=decoded.text,
            raw_text="".join(g.label for g in glyphs),
            confidence=confidence,
            box=Box(int(box.x * inv), int(box.y * inv), int(box.w * inv), int(box.h * inv)),
            quad=(
                [[round(x * inv, 1), round(y * inv, 1)] for x, y in candidate.quad]
                if candidate.quad
                else None
            ),
            country=country,
            region=region,
            plate_type=plate_type,
            format_name=decoded.format.name,
            format_valid=decoded.valid,
            glyphs=mapped_glyphs,
            alternates=grammar.alternates(probabilities, decoded),
            diagnostics={
                "detector": candidate.strategy,
                "detector_score": round(candidate.score, 4),
                "glyph_count": len(glyphs),
                "glyph_confidence": round(glyph_conf, 4),
                "quality": round(quality, 3),
                "region_name": grammar.describe_region(region, decoded.text),
                "segmentation": seg_diag,
                "grammar_score": decoded.score,
                "target_height": target_h,
            },
        )


def _rank_key(reading: PlateReading) -> tuple[float, float]:
    """Prefer format-valid, high-confidence reads when fusing hypotheses."""
    return (1.0 if reading.format_valid else 0.0, reading.confidence)


def _glyph_confidence(probabilities: list[float], top1_mean: float) -> float:
    if not probabilities:
        return 0.0
    geometric = float(np.exp(np.mean(np.log(np.clip(probabilities, 1e-4, 1.0)))))
    return float(np.clip(0.55 * geometric + 0.45 * top1_mean, 0.0, 1.0))


def _same_plate(a: PlateReading, b: PlateReading) -> bool:
    return grammar.normalize(a.plate) == grammar.normalize(b.plate) or _iou(a.box, b.box) > 0.6


def _dedupe(plates: list[PlateReading]) -> list[PlateReading]:
    kept: list[PlateReading] = []
    for plate in plates:
        duplicate = False
        for other in kept:
            if grammar.normalize(plate.plate) == grammar.normalize(other.plate):
                duplicate = True
                break
            iou = _iou(plate.box, other.box)
            if iou > 0.6:
                duplicate = True
                break
        if not duplicate:
            kept.append(plate)
    return kept


def _iou(a: Box, b: Box) -> float:
    x0, y0 = max(a.x, b.x), max(a.y, b.y)
    x1, y1 = min(a.x2, b.x2), min(a.y2, b.y2)
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    union = a.area + b.area - inter
    return inter / union if union else 0.0


_engine: ALPREngine | None = None


def get_engine() -> ALPREngine:
    global _engine
    if _engine is None:
        _engine = ALPREngine()
    return _engine
