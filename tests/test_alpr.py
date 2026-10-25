"""Engine, grammar and imaging correctness."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.alpr import grammar, imaging, ocr
from app.alpr.engine import EngineOptions, get_engine
from app.core.errors import ValidationFailed
from app.services import samples

ROOT = Path(__file__).resolve().parent.parent


def _frame(text: str, kind: str = "private") -> bytes:
    plate = samples.render_plate(text, kind=kind, width=430)
    canvas = Image.new("RGB", (640, 300), (34, 36, 42))
    canvas.paste(plate, ((640 - plate.width) // 2, (300 - plate.height) // 2))
    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=94)
    return buffer.getvalue()


# ------------------------------------------------------------------- grammar
def test_indian_format_is_valid():
    fmt = grammar.FORMATS_BY_NAME["IN_STANDARD"]
    assert grammar.validate("MH12AB3456", fmt)
    assert not grammar.validate("MH1AB3456", fmt)
    assert not grammar.validate("MHI2AB3456", fmt)


def test_generic_formats_accept_freeform():
    generic = grammar.FORMATS_BY_NAME["GENERIC_ALNUM"]
    assert grammar.validate("AB1234", generic)
    assert not grammar.validate("AB12", grammar.FORMATS_BY_NAME["UK_STANDARD"])


def test_decode_recovers_plate_from_noisy_probabilities():
    truth = "MH12AB3456"
    probabilities = []
    for char in truth:
        distribution = {char: 0.92}
        for other in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            distribution.setdefault(other, 0.008)
        probabilities.append(distribution)
    decoded = grammar.decode(probabilities, country="IN")
    assert decoded is not None
    assert decoded.text == truth
    assert decoded.format.country in ("IN",)


def test_decode_skips_leading_junk_glyph():
    truth = "KA05MJ4412"
    junk = {"G": 0.16, "S": 0.14, "N": 0.09, "5": 0.07}
    probabilities = [dict.fromkeys("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0.005) | junk]
    for char in truth:
        probabilities.append(dict.fromkeys("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0.004) | {char: 0.94})
    decoded = grammar.decode(probabilities, country="IN")
    assert decoded is not None
    assert decoded.text == truth


def test_region_and_type_inference():
    assert grammar.infer_region("OD02BV7788", "IN") == "OD"
    assert "Odisha" in grammar.describe_region("OD", "OD02BV7788")
    assert grammar.infer_plate_type("MH12AB3456", grammar.FORMATS_BY_NAME["IN_STANDARD"]) == "private"


def test_risk_score_bands():
    low, low_level = grammar.risk_score(watchlist_hit=False, confidence=0.9)
    high, high_level = grammar.risk_score(
        watchlist_hit=True, watchlist_severity="critical", confidence=0.4
    )
    assert low_level == "low"
    assert high > low
    assert high_level in ("high", "critical")


# ------------------------------------------------------------------- imaging
def test_image_roundtrip_and_hash():
    payload = _frame("MH12AB3456")
    image = imaging.load_image(payload)
    array = imaging.to_numpy(image)
    assert array.shape[2] == 3
    assert imaging.average_hash(array) == imaging.average_hash(array)
    assert len(imaging.average_hash(array)) == 64
    thumb = imaging.thumbnail(array, 200)
    assert Image.open(io.BytesIO(thumb)).width <= 200


def test_invalid_payload_rejected():
    with pytest.raises(ValidationFailed):
        imaging.load_image(b"not-an-image")


def test_text_line_components_finds_characters():
    payload = _frame("UP32KL1190")
    array = imaging.to_numpy(imaging.load_image(payload))
    gray = imaging.to_gray(array)
    mask, _info = imaging.best_ink_mask(gray[60:240, 150:490], target_h=96)
    line, line_info = imaging.text_line_components(mask, 96)
    assert len(line) >= 8
    assert line_info["score"] > 0.5


# -------------------------------------------------------------------- engine
def test_engine_reads_clean_plates_accurately():
    engine = get_engine()
    truth = ["MH12AB3456", "DL08CD9021", "TS09EF2266", "AP39TR7712"]
    hits = 0
    for text in truth:
        result = engine.recognize(_frame(text), EngineOptions(country="IN"))
        if result.best and result.best.plate == text:
            hits += 1
    assert hits >= 3, f"expected at least 3/4 exact reads, got {hits}"


def test_engine_returns_geometry_and_glyphs():
    engine = get_engine()
    result = engine.recognize(_frame("MH12AB3456"), EngineOptions(country="IN"))
    best = result.best
    assert best is not None
    assert best.confidence > 0.5
    assert best.box.w > 100 and best.box.h > 20
    assert len(best.glyphs) >= 9
    assert all(0 <= glyph.confidence <= 1 for glyph in best.glyphs)
    assert result.processing_ms > 0
    assert result.engine_version.startswith("pv-alpr/")


def test_engine_handles_blank_frames():
    canvas = Image.new("RGB", (640, 400), (18, 18, 20))
    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG")
    result = get_engine().recognize(buffer.getvalue())
    assert result.best is None
    assert result.plates == []


def test_glyph_bank_is_cached():
    bank = ocr.TemplateBank()
    bank.ensure()
    assert len(bank.label_set if hasattr(bank, "label_set") else bank.labels) > 0
    assert len(bank.class_index) >= 30


def test_structural_descriptor_detects_holes():
    eight = np.zeros((48, 32), dtype=np.float32)
    eight[6:42, 6:26] = 1.0
    eight[16:32, 12:20] = 0.0
    descriptor = ocr.structural_descriptor(eight)
    assert descriptor["fill"] > 0.4


def test_annotate_produces_jpeg():
    from app.services.annotate import annotate

    payload = _frame("MH12AB3456")
    image = imaging.load_image(payload)
    result = get_engine().recognize(payload, EngineOptions(country="IN"))
    overlay = annotate(image, result)
    assert overlay[:2] == b"\xff\xd8"
    assert Image.open(io.BytesIO(overlay)).size == image.size


# --------------------------------------------------------------- benchmark
def test_reader_survives_the_degradation_matrix():
    """A regression floor over the real benchmark corpus.

    The floors sit well below the measured values (61% / 81% over 1000 frames),
    so this fails on a genuine regression rather than on corpus noise. It is
    deliberately small — the full sweep lives in ``scripts/bench_alpr.py`` and
    is too slow for every CI run.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from bench_alpr import run

    report = run(120, verbose=False)
    stats = report.summary()

    assert stats["exact_accuracy"] >= 50.0, stats["exact_accuracy"]
    assert stats["character_accuracy"] >= 72.0, stats["character_accuracy"]
    assert stats["false_positive_rate"] <= 6.0, stats["false_positive_rate"]


def test_benchmark_flags_tilted_plates():
    """A plate rotated past the level detector's reach must still be found."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from bench_alpr import MATRIX, render_frame

    tilted = next(item for item in MATRIX if item.name == "tilt-16")
    hits = 0
    for seed in range(1, 5):
        payload = render_frame("MH12AB3456", kind="private", condition=tilted, seed=seed)
        result = get_engine().recognize(payload, EngineOptions(country="IN"))
        hits += bool(result.plates)
    assert hits >= 3, f"detector missed {4 - hits}/4 tilted frames"
