"""ALPR accuracy harness.

Runs the shipped engine over a deterministic synthetic corpus and prints
character-level and exact-match accuracy, plus latency percentiles. Used in CI
(:mod:`tests.test_alpr`) and by operators validating a new glyph bank.

    python scripts/eval_alpr.py
    python scripts/eval_alpr.py --degraded
"""
from __future__ import annotations

import argparse
import io
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from app.alpr.engine import EngineOptions, get_engine
from app.services import samples


def _clean_frame(text: str, kind: str) -> bytes:
    plate = samples.render_plate(text, kind=kind, width=420)
    canvas = Image.new("RGB", (620, 300), (38, 40, 46))
    canvas.paste(plate, ((620 - plate.width) // 2, (300 - plate.height) // 2))
    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def evaluate(degraded: bool = False) -> dict[str, float]:
    engine = get_engine()
    # Warm the glyph bank and JIT paths so the numbers reflect steady state.
    engine.recognize(_clean_frame("MH12AB3456", "private"), EngineOptions(country="IN"))
    exact = 0
    char_correct = 0
    char_total = 0
    latencies: list[float] = []
    failures: list[tuple[str, str]] = []
    for index, (text, _country, kind) in enumerate(samples.PLATE_POOL):
        payload = (
            samples.sample_bytes(f"sample-{index + 1}") if degraded else _clean_frame(text, kind)
        )
        started = time.perf_counter()
        result = engine.recognize(payload, EngineOptions(country="IN"))
        latencies.append((time.perf_counter() - started) * 1000)
        predicted = result.best.plate if result.best else ""
        if predicted == text:
            exact += 1
        else:
            failures.append((text, predicted or "-"))
        char_total += len(text)
        char_correct += sum(1 for a, b in zip(text, predicted, strict=False) if a == b)
    total = len(samples.PLATE_POOL)
    ordered = sorted(latencies)
    return {
        "frames": total,
        "exact_matches": exact,
        "exact_accuracy": round(100 * exact / total, 1),
        "character_accuracy": round(100 * char_correct / char_total, 1),
        "latency_avg_ms": round(statistics.mean(latencies), 1),
        "latency_p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
        "failures": failures,  # type: ignore[dict-item]
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the ALPR engine")
    parser.add_argument("--degraded", action="store_true", help="use camera-like degraded frames")
    args = parser.parse_args()
    report = evaluate(degraded=args.degraded)
    print(f"frames            : {report['frames']}")
    print(f"exact matches     : {report['exact_matches']} ({report['exact_accuracy']}%)")
    print(f"character accuracy: {report['character_accuracy']}%")
    print(f"latency avg / p95 : {report['latency_avg_ms']} ms / {report['latency_p95_ms']} ms")
    if report["failures"]:
        print("misses:")
        for expected, got in report["failures"]:  # type: ignore[misc]
            print(f"  {expected:<14} -> {got}")


if __name__ == "__main__":
    main()
