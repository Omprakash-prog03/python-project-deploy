"""End-to-end ALPR engine: decode -> detect -> rectify -> segment -> classify -> decode grammar."""
from __future__ import annotations

import time
from dataclasses import dataclass, replace

import numpy as np

from app.alpr import detect, grammar, imaging, ocr
from app.alpr.types import Box, Glyph, PlateReading, RecognitionResult
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)
ENGINE_VERSION = "pv-alpr/1.3"

# Below this confidence a read that also fails every known plate grammar is
# treated as noise rather than as a plate.
UNVERIFIED_FLOOR = 0.55


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
            # A read that fits no jurisdiction's grammar and is not confident
            # either is a guess, and a confident-looking wrong plate is worse
            # than no plate at all downstream. Drop it here rather than letting
            # it reach an operator who has no way to tell it apart from a real
            # one.
            if not reading.format_valid and reading.confidence < UNVERIFIED_FLOOR:
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

        aspect = max(1.4, min(7.5, _candidate_aspect(candidate)))
        readings: list[PlateReading] = []
        for variant in self._rectify_variants(gray, crop, candidate, aspect):
            reading = self._read_rectified(variant[0], variant[1], box, candidate, opts, scale)
            if reading is None:
                continue
            readings.append(reading)
            # Only a unanimous-looking read is worth stopping for. A single
            # confident variant is not enough evidence when the frame is
            # degraded, and that is precisely when a second opinion pays.
            if (
                reading.format_valid
                and reading.confidence >= 0.9
                and _min_glyph_confidence(reading) >= 0.72
            ):
                break
        return _consensus(readings)

    @staticmethod
    def _rectify_variants(
        gray: np.ndarray,
        crop: np.ndarray,
        candidate: detect.Candidate,
        aspect: float,
    ):
        """Yield (rectified_image, target_height) hypotheses, best first.

        Resolution matters more than it looks. A plate 40 px tall in the frame
        is only ~5 px per stroke; downsampling that to a 96 px strip throws away
        the very edges the segmenter needs, and a blurred plate loses them
        twice. Upscaling the small ones and sharpening the result keeps the
        strokes separable, which is the difference between ``MH12AB3456`` and
        ``MH12M3456``. Big crops still go through a cheap downscale first,
        because a 400 px plate does not need interpolation to read well.
        """
        # Two hypotheses per candidate: the reader gets a second opinion
        # without paying for a third, and consensus still has something to
        # compare. Three resolutions measured slower without reading better.
        source_h = crop.shape[0]
        heights = (192, 128) if source_h < 140 else (128, 192)
        for target_h in heights:
            target_w = int(min(900, max(72, target_h * aspect)))
            resized = imaging.resize(crop, (target_w, target_h))
            if target_h >= 192:
                # LANCZOS-style upscale leaves soft edges; one unsharp pass
                # restores the contrast the glyph bank was rendered with.
                yield imaging.sharpen(resized, amount=0.9), target_h
            else:
                yield resized, target_h
            if candidate.quad and len(candidate.quad) == 4:
                warped = imaging.warp_perspective(gray, candidate.quad, (target_w, target_h))
                if warped.size:
                    if target_h >= 192:
                        warped = imaging.sharpen(warped, amount=0.9)
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


def _candidate_aspect(candidate: detect.Candidate) -> float:
    """Plate aspect from the rotated rect when one exists.

    ``box`` is the axis-aligned bound, so a tilted plate reads as ~1.8:1 instead
    of its real ~3.2:1. Using that number to size the rectified crop squashes
    the glyphs horizontally, which is one of the largest single sources of
    misreads on angled captures.
    """
    quad = candidate.quad
    if quad and len(quad) == 4:
        edges = []
        for index in range(4):
            x0, y0 = quad[index]
            x1, y1 = quad[(index + 1) % 4]
            edges.append((((x1 - x0) ** 2) + ((y1 - y0) ** 2)) ** 0.5)
        long_side, short_side = max(edges), max(1e-6, min(edges))
        return float(long_side / short_side)
    return candidate.box.w / max(1, candidate.box.h)


def _min_glyph_confidence(reading: PlateReading) -> float:
    if not reading.glyphs:
        return 0.0
    return min(glyph.confidence for glyph in reading.glyphs)


def _consensus(readings: list[PlateReading]) -> PlateReading | None:
    """Pick the reading the variants agree on, not just the single best one.

    Every candidate is read several times — at two or three resolutions, straight
    and perspective-warped. Those variants fail *independently*: blur that turns
    a ``B`` into an ``8`` at 96 px often reads correctly at 192 px, and the warp
    fixes errors the straight crop introduces. Taking only the top-ranked
    variant throws that redundancy away and lets one bad hypothesis decide the
    plate.

    Grouping the variants and letting agreement win turns it into a small
    ensemble: a string read by three variants beats a higher-scoring string read
    by one, which is what removes the random ``B``/``8`` flips.
    """
    if not readings:
        return None
    if len(readings) == 1:
        return readings[0]

    groups: dict[str, list[PlateReading]] = {}
    for reading in readings:
        groups.setdefault(grammar.normalize(reading.plate), []).append(reading)

    def group_key(members: list[PlateReading]) -> tuple[float, float, float]:
        # Agreement first, then summed evidence, then best single read.
        return (
            float(len(members)),
            sum(member.confidence for member in members),
            max(member.confidence for member in members),
        )

    winner = max(groups.values(), key=group_key)
    winner = _prefer_untruncated(winner, groups)
    chosen = max(winner, key=_rank_key)
    if len(winner) == len(readings) and len(winner) > 1:
        # Unanimous: every variant independently arrived at the same plate.
        boost = 1.0 + min(0.06, 0.02 * (len(winner) - 1))
        chosen = replace(chosen, confidence=float(min(0.995, chosen.confidence * boost)))
    elif len(winner) == 1:
        # A lone dissenting read is the least trustworthy outcome of all, so it
        # is damped rather than presented at face value.
        chosen = replace(chosen, confidence=chosen.confidence * 0.86)
    return chosen


def _is_subsequence(short: str, long: str) -> bool:
    iterator = iter(long)
    return all(char in iterator for char in short)


def _prefer_untruncated(
    winner: list[PlateReading], groups: dict[str, list[PlateReading]]
) -> list[PlateReading]:
    """Prefer a complete read over a truncated one that merely got more votes.

    A clipped leading or trailing character is *systematic*: the crop is the
    same for every variant, so they all lose the same character and agreement
    cannot rescue it. Worse, consensus then actively promotes the truncated
    string because it is self-consistent. A plate that is a strict subsequence
    of a format-valid rival is almost always the clipped version, so the rival
    wins whenever it has comparable support.
    """
    best = max(winner, key=_rank_key)
    if not best.format_valid:
        return winner
    for members in groups.values():
        if members is winner or len(members) < len(winner) - 1:
            continue
        rival = max(members, key=_rank_key)
        if not rival.format_valid or len(rival.plate) <= len(best.plate):
            continue
        if _is_subsequence(grammar.normalize(best.plate), grammar.normalize(rival.plate)):
            return members
    return winner


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


_engine: FallbackEngine | ALPREngine | None = None


class FallbackEngine:
    """Tries the remote reader first, and answers locally when it cannot.

    Wrapping rather than replacing is the whole point: every caller already
    speaks ``recognize(bytes, options)``, so this is the only place that has to
    know a remote backend exists. If the API is down, rate-limited, slow or
    simply not configured, the request still returns a plate.
    """

    def __init__(self, primary, local: ALPREngine) -> None:
        self.primary = primary
        self.local = local
        self.version = f"{local.version}+remote"

    @property
    def classifier(self):
        # The health check inspects the local classifier; keep the attribute.
        return self.local.classifier

    @property
    def active_engine(self) -> str:
        return getattr(self.primary, "name", "remote") if self.primary else "local"

    def recognize(self, image_bytes: bytes, options: EngineOptions | None = None) -> RecognitionResult:
        if self.primary is not None:
            try:
                result = self.primary.recognize(image_bytes, options)
            except Exception as exc:  # any remote fault falls back
                logger.warning(
                    "remote engine failed, using local pipeline",
                    extra={"error": str(exc)}, exc_info=False,
                )
                result = None
            if result is not None:
                if result.plates:
                    result.diagnostics.setdefault("engine", getattr(self.primary, "name", "remote"))
                    return result
                # A confident empty answer is still an answer: the cloud looked
                # and found no plate. Keep it rather than guessing locally.
                result.diagnostics["engine"] = getattr(self.primary, "name", "remote")
                return result

        result = self.local.recognize(image_bytes, options)
        result.diagnostics.setdefault("engine", "local")
        return result


def get_engine() -> FallbackEngine | ALPREngine:
    global _engine
    if _engine is None:
        local = ALPREngine()
        if settings.parkpow_configured:
            from app.alpr.remote import ParkPowEngine

            remote = ParkPowEngine(
                settings.parkpow_api_key,
                base_url=settings.parkpow_base_url,
                timeout=settings.parkpow_timeout_seconds,
                regions=tuple(settings.parkpow_region_list),
                camera_id=settings.parkpow_camera_id,
                retries=settings.parkpow_retries,
            )
            _engine = FallbackEngine(remote, local)
            logger.info("remote reader enabled", extra={"endpoint": remote.endpoint})
        else:
            if settings.alpr_engine == "parkpow":
                logger.warning(
                    "ALPR_ENGINE=parkpow but PARKPOW_API_KEY is empty; running the local engine"
                )
            _engine = local
    return _engine
