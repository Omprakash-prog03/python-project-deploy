"""ALPR benchmark: a wide corpus, a degradation matrix, and honest numbers.

The old ``eval_alpr.py`` runs the 15 plates in ``samples.PLATE_POOL`` through
the engine, which is a smoke test rather than a measurement: the corpus is tiny
and identical to what the glyph bank was tuned on. This script instead renders
a much larger corpus and pushes every frame through a *matrix* of conditions a
real camera actually produces — tilt, motion blur, distance, sensor noise, bad
JPEG, low light, dirt — then reports accuracy per condition.

Usage::

    python scripts/bench_alpr.py                 # 1000 rounds, ~2-4 minutes
    python scripts/bench_alpr.py --rounds 200    # quick pass
    python scripts/bench_alpr.py --json out.json # machine-readable report
    python scripts/bench_alpr.py --conditions    # per-condition table only

Numbers to read:

* **exact match** — the whole plate string came back correct. This is the only
  metric a customer cares about; a plate is right or it is not.
* **character accuracy** — per-character, useful for spotting confusion pairs.
* **false positives** — a read emitted for a frame containing no plate at all.
  In a tolling deployment this is the metric that gets you escalated, because a
  confident wrong plate is worse than no plate.
* **p95 latency** — tail behaviour on the slowest frames.

The corpus is synthetic and rendered from the same font family the template bank
uses, so these numbers are an *upper bound* on field accuracy. They are still
directly comparable to each other, which is what makes them useful for deciding
whether a change helped.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import io

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from app.alpr.engine import EngineOptions, get_engine
from app.services import samples

# Plates drawn from the grammars the engine claims to support.
CORPUS: dict[str, list[tuple[str, str]]] = {
    "IN": [
        ("MH12AB3456", "private"), ("OD02BV7788", "private"), ("KA05MJ4412", "private"),
        ("DL08CD9021", "private"), ("TN01ZZ6633", "private"), ("UP32KL1190", "private"),
        ("GJ01WT8090", "commercial"), ("RJ14GH2205", "private"), ("HR26DK8337", "private"),
        ("MP09QR5174", "private"), ("PB10FM4481", "private"), ("WB02AC3390", "private"),
        ("AP39TR7712", "commercial"), ("TS09EF2266", "private"), ("KL07CM5108", "private"),
        ("MH14GN4417", "private"), ("CH01AL9823", "private"), ("GA03WQ1266", "private"),
        ("BR01PA7714", "private"), ("JH01CX9920", "private"),
    ],
    "US": [
        ("7XE4821", "private"), ("AJD4810", "private"), ("KLR2290", "private"),
        ("8GTR550", "private"), ("MBZ1174", "private"),
    ],
    "UK": [
        ("AB12CDE", "private"), ("LR65JKX", "private"), ("YE19OSU", "private"),
        ("BX71GHD", "private"), ("KM08NPR", "private"),
    ],
    "EU": [
        ("BXA2184", "private"), ("MAB4170", "private"), ("KA3931", "private"),
        ("HHAB882", "private"), ("DGH7721", "private"),
    ],
}

SCENES = ("dusk", "night", "tunnel", "highway")


@dataclass(slots=True)
class Condition:
    """One point in the degradation matrix."""

    name: str
    angle: float = 0.0
    blur: float = 0.7
    scale: float = 1.0
    noise: float = 5.0
    jpeg: int = 84
    scene: str | None = None
    dirt: float = 0.0
    downscale: float = 1.0


MATRIX: tuple[Condition, ...] = (
    Condition("clean"),
    Condition("tilt-8", angle=8.0),
    Condition("tilt-16", angle=16.0),
    Condition("tilt-24", angle=24.0),
    Condition("motion-blur", blur=2.2),
    Condition("heavy-blur", blur=3.4),
    Condition("distant", scale=0.55),
    Condition("very-distant", scale=0.38),
    Condition("sensor-noise", noise=18.0),
    Condition("night", noise=12.0, scene="night", jpeg=72),
    Condition("low-jpeg", jpeg=38),
    Condition("dirty-plate", dirt=0.55),
    Condition("downscaled", downscale=0.34),
    Condition("hard-mix", angle=13.0, blur=2.0, scale=0.62, noise=14.0, jpeg=58, scene="night"),
)


@dataclass
class Round:
    condition: str
    country: str
    expected: str
    predicted: str = ""
    confidence: float = 0.0
    ms: float = 0.0
    format_valid: bool = False
    blank: bool = False


@dataclass
class Report:
    rounds: list[Round] = field(default_factory=list)

    def summary(self) -> dict:
        graded = [r for r in self.rounds if not r.blank]
        blanks = [r for r in self.rounds if r.blank]
        exact = sum(1 for r in graded if r.predicted == r.expected)
        chars = sum(len(r.expected) for r in graded)
        correct_chars = sum(
            sum(1 for a, b in zip(r.expected, r.predicted, strict=False) if a == b) for r in graded
        )
        latencies = sorted(r.ms for r in self.rounds)
        false_positives = sum(1 for r in blanks if r.predicted and r.confidence >= 0.5)
        return {
            "frames": len(graded),
            "exact_matches": exact,
            "exact_accuracy": round(100 * exact / max(1, len(graded)), 2),
            "character_accuracy": round(100 * correct_chars / max(1, chars), 2),
            "blank_frames": len(blanks),
            "false_positives": false_positives,
            "false_positive_rate": round(100 * false_positives / max(1, len(blanks)), 2),
            "latency_p50_ms": round(latencies[len(latencies) // 2], 1) if latencies else 0.0,
            "latency_p95_ms": (
                round(latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))], 1)
                if latencies
                else 0.0
            ),
        }

    def by_condition(self) -> dict[str, dict]:
        grouped: dict[str, list[Round]] = defaultdict(list)
        for item in self.rounds:
            grouped[item.condition].append(item)
        out: dict[str, dict] = {}
        for name, items in grouped.items():
            sub = Report(items)
            stats = sub.summary()
            out[name] = {
                "frames": stats["frames"],
                "exact": stats["exact_accuracy"],
                "char": stats["character_accuracy"],
            }
        return out

    def confusions(self, limit: int = 12) -> list[tuple[str, str, int]]:
        counter: Counter[tuple[str, str]] = Counter()
        for item in self.rounds:
            if item.blank or item.predicted == item.expected:
                continue
            for a, b in zip(item.expected, item.predicted, strict=False):
                if a != b:
                    counter[(a, b)] += 1
        return [(a, b, n) for (a, b), n in counter.most_common(limit)]


def _glyph_edges(image: Image.Image, rng: random.Random, amount: float) -> Image.Image:
    """Wear the print: chips and streaks in the ink, so the plate is not pristine."""
    if amount <= 0:
        return image
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for _ in range(int(amount * 26)):
        x = rng.randint(0, image.width)
        y = rng.randint(0, image.height)
        w = rng.randint(2, max(3, image.width // 22))
        h = rng.randint(1, max(2, image.height // 14))
        draw.ellipse([x, y, x + w, y + h], fill=(120, 120, 115, rng.randint(90, 190)))
    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def render_frame(
    text: str,
    *,
    kind: str,
    condition: Condition,
    seed: int,
    with_plate: bool = True,
) -> bytes:
    """One camera frame: background, optional plate, then the condition's damage."""
    rng = random.Random(seed)
    width, height = 960, 600
    scene = condition.scene or SCENES[seed % len(SCENES)]
    top, bottom = samples.SCENES.get(scene, samples.SCENES["dusk"])
    gradient = np.zeros((height, width, 3), dtype=np.float32)
    for y in range(height):
        ratio = y / max(1, height - 1)
        gradient[y, :, :] = np.array(top) * (1 - ratio) + np.array(bottom) * ratio
    # Vehicle body, so the plate sits on something rather than floating.
    body = np.array(
        [rng.randint(22, 66), rng.randint(24, 70), rng.randint(32, 84)], dtype=np.float32
    )
    gradient[int(height * 0.40) :, int(width * 0.10) : int(width * 0.90)] = body * 0.9
    frame = np.clip(
        gradient + np.random.default_rng(seed).normal(0, 4.0, gradient.shape), 0, 255
    ).astype(np.uint8)
    image = Image.fromarray(frame)

    if with_plate:
        plate_w = int(rng.randint(360, 430) * condition.scale)
        plate = samples.render_plate(text, kind=kind, width=max(120, plate_w))
        if condition.dirt:
            plate = _glyph_edges(plate, rng, condition.dirt)
        if condition.angle:
            plate = plate.rotate(
                condition.angle * (1 if seed % 2 else -1), resample=Image.BICUBIC, expand=True
            )
        # Perspective yaw on top of the roll.
        yaw = rng.uniform(0.86, 1.0)
        plate = plate.resize((plate.width, max(18, int(plate.height * yaw))), Image.BICUBIC)
        px = rng.randint(int(width * 0.16), max(int(width * 0.16) + 1, int(width * 0.62)))
        py = rng.randint(int(height * 0.26), int(height * 0.42))
        image.paste(plate, (px, py))

    image = image.filter(ImageFilter.GaussianBlur(radius=max(0.1, condition.blur)))
    array = np.array(image, dtype=np.float32)
    if condition.noise:
        array = np.clip(
            array + np.random.default_rng(seed + 7).normal(0, condition.noise, array.shape), 0, 255
        )
    image = Image.fromarray(array.astype(np.uint8))
    if condition.downscale < 1.0:
        small = (
            max(64, int(width * condition.downscale)),
            max(40, int(height * condition.downscale)),
        )
        image = image.resize(small, Image.BILINEAR)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=int(condition.jpeg))
    return buffer.getvalue()


def build_rounds(rounds: int, *, include_blanks: bool = True) -> list[Round]:
    """Plan the run: rotate through plates, countries and conditions."""
    plan: list[Round] = []
    countries = list(CORPUS)
    index = 0
    while len(plan) < rounds:
        country = countries[index % len(countries)]
        plate, _kind = CORPUS[country][(index // len(countries)) % len(CORPUS[country])]
        condition = MATRIX[index % len(MATRIX)]
        plan.append(
            Round(
                condition=condition.name,
                country=country,
                expected=plate,
            )
        )
        index += 1
    if include_blanks:
        # Empty frames: the engine must stay silent rather than invent a plate.
        for blank_index in range(max(8, rounds // 12)):
            plan.append(
                Round(
                    condition=f"blank-{MATRIX[blank_index % len(MATRIX)].name}",
                    country="IN",
                    expected="",
                    blank=True,
                )
            )
    return plan


def run(rounds: int = 1000, *, verbose: bool = True) -> Report:
    engine = get_engine()
    plan = build_rounds(rounds)
    conditions = {c.name: c for c in MATRIX}
    report = Report()
    started = time.perf_counter()
    # Warm the glyph bank so the first measurement is not the build cost.
    engine.recognize(
        render_frame("MH12AB3456", kind="private", condition=MATRIX[0], seed=1),
        EngineOptions(country="IN"),
    )
    for number, item in enumerate(plan, start=1):
        if item.blank:
            base = conditions[item.condition.removeprefix("blank-")]
            condition = Condition(
                name=item.condition, blur=base.blur, noise=base.noise, jpeg=base.jpeg,
                scene=base.scene,
            )
            payload = render_frame("", kind="private", condition=condition, seed=number,
                                   with_plate=False)
        else:
            condition = conditions[item.condition]
            kind = next(
                (k for t, k in CORPUS[item.country] if t == item.expected), "private"
            )
            payload = render_frame(item.expected, kind=kind, condition=condition, seed=number)
        clock = time.perf_counter()
        result = engine.recognize(payload, EngineOptions(country=item.country))
        item.ms = (time.perf_counter() - clock) * 1000
        best = result.best
        item.predicted = best.plate if best else ""
        item.confidence = best.confidence if best else 0.0
        item.format_valid = bool(best.format_valid) if best else False
        report.rounds.append(item)
        if verbose and number % 100 == 0:
            elapsed = time.perf_counter() - started
            print(f"  {number}/{len(plan)} frames · {elapsed:.0f}s", file=sys.stderr)
    return report


def print_report(report: Report) -> None:
    stats = report.summary()
    print()
    print(f"frames graded     : {stats['frames']}")
    print(f"exact match       : {stats['exact_matches']}  ({stats['exact_accuracy']}%)")
    print(f"character accuracy: {stats['character_accuracy']}%")
    print(
        f"false positives   : {stats['false_positives']}/{stats['blank_frames']} blank frames "
        f"({stats['false_positive_rate']}%)"
    )
    print(f"latency p50 / p95 : {stats['latency_p50_ms']} / {stats['latency_p95_ms']} ms")
    print()
    print(f"{'condition':<20}{'frames':>7}{'exact':>9}{'char':>9}")
    print("-" * 45)
    for name, row in report.by_condition().items():
        if name.startswith("blank-"):
            continue
        print(f"{name:<20}{row['frames']:>7}{row['exact']:>8.1f}%{row['char']:>8.1f}%")
    confusions = report.confusions()
    if confusions:
        print()
        print("top confusions (expected -> read):")
        for expected, got, count in confusions:
            print(f"  {expected} -> {got}   x{count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark the ALPR engine")
    parser.add_argument("--rounds", type=int, default=1000, help="graded frames (default 1000)")
    parser.add_argument("--json", type=Path, help="write the full report as JSON")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    report = run(args.rounds, verbose=not args.quiet)
    print_report(report)
    if args.json:
        payload = {
            "summary": report.summary(),
            "by_condition": report.by_condition(),
            "confusions": report.confusions(30),
        }
        args.json.write_text(json.dumps(payload, indent=2))
        print(f"\nreport written to {args.json}")


if __name__ == "__main__":
    main()
