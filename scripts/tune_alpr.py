"""Offline tuning of the glyph fusion weights on a held-out synthetic corpus."""
from __future__ import annotations

import io
import itertools
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.alpr import learning
from app.alpr.engine import EngineOptions, get_engine
from app.alpr.ocr import get_classifier
from app.services import samples

STATES = ["MH", "OD", "KA", "DL", "TN", "UP", "GJ", "RJ", "HR", "MP", "PB", "WB", "AP", "TS", "KL"]
LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"


def dev_plates(count: int = 36, seed: int = 7) -> list[str]:
    rng = random.Random(seed)
    plates = []
    while len(plates) < count:
        state = rng.choice(STATES)
        district = f"{rng.randint(1, 99):02d}"
        series = "".join(rng.choice(LETTERS) for _ in range(rng.choice([1, 2])))
        number = f"{rng.randint(1000, 9999)}"
        plates.append(f"{state}{district}{series}{number}")
    return plates


def frame(text: str, *, kind: str = "private", scale: float = 1.0, blur: float = 0.0,
          noise: float = 0.0, bright: float = 1.0, quality: int = 88, seed: int = 0) -> bytes:
    plate = samples.render_plate(text, kind=kind, width=max(160, int(420 * scale)))
    canvas = Image.new("RGB", (620, 300), (38, 40, 46))
    canvas.paste(plate, ((620 - plate.width) // 2, (300 - plate.height) // 2))
    if bright != 1.0:
        canvas = ImageEnhance.Brightness(canvas).enhance(bright)
    if blur:
        canvas = canvas.filter(ImageFilter.GaussianBlur(blur))
    array = np.asarray(canvas).astype(np.float32)
    if noise:
        rng = np.random.default_rng(seed)
        array = np.clip(array + rng.normal(0, noise, array.shape), 0, 255)
    buffer = io.BytesIO()
    Image.fromarray(array.astype(np.uint8)).save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def build_dev() -> list[tuple[str, bytes]]:
    frames: list[tuple[str, bytes]] = []
    for index, text in enumerate(dev_plates()):
        frames.append((text, frame(text)))
        frames.append((text, frame(text, scale=0.8, blur=1.1, noise=4.0, quality=72, seed=index)))
    return frames


def score(frames: list[tuple[str, bytes]]) -> tuple[int, float]:
    engine = get_engine()
    exact = 0
    chars = total = 0
    for truth, payload in frames:
        best = engine.recognize(payload, EngineOptions(country="IN")).best
        got = best.plate if best else ""
        exact += got == truth
        chars += sum(1 for a, b in zip(truth, got, strict=False) if a == b)
        total += len(truth)
    return exact, round(100 * chars / total, 1)


def main() -> None:
    learning.reset()
    engine = get_engine()
    engine.recognize(frame("MH12AB3456"), EngineOptions(country="IN"))  # warm
    classifier = get_classifier()
    dev = build_dev()
    official_frames = [
        (text, frame(text, kind=kind)) for text, _country, kind in samples.PLATE_POOL
    ]
    print(f"dev frames: {len(dev)} · official frames: {len(official_frames)}")

    results = []
    for tw, temp, struct in itertools.product((0.45, 0.55, 0.65, 0.75), (0.045, 0.055, 0.07), (0.0, 0.5, 1.0)):
        classifier.template_weight = tw
        classifier.knn_weight = 1.0 - tw
        classifier.temperature = temp
        classifier.structure_weight = struct
        dev_exact, dev_char = score(dev)
        results.append((dev_char, dev_exact, tw, temp, struct))
        print(f"tw={tw:.2f} temp={temp:.3f} struct={struct:.1f} -> dev {dev_exact}/{len(dev)} · {dev_char}%")

    results.sort(reverse=True)
    print("\nbest dev configs:")
    for row in results[:5]:
        print(row)

    for _char, _exact, tw, temp, struct in results[:5]:
        classifier.template_weight = tw
        classifier.knn_weight = 1.0 - tw
        classifier.temperature = temp
        classifier.structure_weight = struct
        ex, ch = score(official_frames)
        print(f"official with tw={tw} temp={temp} struct={struct}: {ex}/15 · {ch}%")


if __name__ == "__main__":
    main()
