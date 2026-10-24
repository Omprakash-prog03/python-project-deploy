"""Synthetic capture generator.

Produces realistic, plate-bearing frames for demos, onboarding and CI. The
images are generated from the same font bank the classifier uses, then degraded
(perspective, motion blur, sensor noise, JPEG) so the demo exercises the real
pipeline end to end instead of returning canned JSON.
"""
from __future__ import annotations

import io
import math
import random
from datetime import UTC, datetime

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.core.config import PROJECT_ROOT

FONT_BOLD = PROJECT_ROOT / "app" / "assets" / "fonts" / "DejaVuSans-Bold.ttf"
FONT_REG = PROJECT_ROOT / "app" / "assets" / "fonts" / "DejaVuSans.ttf"

SCENES: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "dusk": ((28, 32, 58), (96, 74, 120)),
    "night": ((8, 10, 20), (34, 38, 66)),
    "tunnel": ((22, 22, 26), (74, 66, 70)),
    "highway": ((92, 108, 132), (188, 196, 210)),
}

PLATE_POOL = [
    ("MH12AB3456", "IN", "private"),
    ("OD02BV7788", "IN", "private"),
    ("KA05MJ4412", "IN", "private"),
    ("DL08CD9021", "IN", "private"),
    ("TN01ZZ6633", "IN", "private"),
    ("UP32KL1190", "IN", "private"),
    ("GJ01WT8090", "IN", "commercial"),
    ("RJ14GH2205", "IN", "private"),
    ("HR26DK8337", "IN", "private"),
    ("MP09QR5174", "IN", "private"),
    ("PB10FM4481", "IN", "private"),
    ("WB02AC3390", "IN", "private"),
    ("AP39TR7712", "IN", "commercial"),
    ("TS09EF2266", "IN", "private"),
    ("KL07CM5108", "IN", "private"),
]


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REG
    try:
        return ImageFont.truetype(str(path), size)
    except Exception:  # pragma: no cover
        return ImageFont.load_default()


def render_plate(text: str, *, kind: str = "private", width: int = 720) -> Image.Image:
    """Draw a clean, HSRP-style plate face."""
    height = int(width * 0.315)
    background = (252, 250, 242) if kind != "commercial" else (250, 226, 128)
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    border = max(3, width // 170)
    draw.rounded_rectangle(
        [border, border, width - border, height - border],
        radius=max(6, width // 60),
        outline=(18, 18, 22),
        width=border,
    )
    band_w = int(width * 0.092)
    draw.rectangle([border, border, border + band_w, height - border], fill=(20, 52, 140))
    draw.text((border + band_w * 0.18, border + height * 0.06), "IND", fill=(255, 255, 255),
              font=_font(int(height * 0.17)))
    draw.ellipse(
        [border + band_w * 0.22, height * 0.47, border + band_w * 0.74, height * 0.68],
        outline=(255, 255, 255),
        width=max(2, border // 2),
    )
    ascii_text = "".join(ch for ch in text.upper() if ch.isalnum())
    text_area_left = border + band_w + int(width * 0.045)
    text_area_width = width - text_area_left - int(width * 0.045)
    font_size = int(height * 0.62)
    while font_size > 12:
        font = _font(font_size)
        measured = draw.textlength(ascii_text, font=font)
        if measured <= text_area_width:
            break
        font_size = int(font_size * 0.94)
    font = _font(font_size)
    bbox = draw.textbbox((0, 0), ascii_text, font=font)
    text_h = bbox[3] - bbox[1]
    draw.text(
        (text_area_left, (height - text_h) / 2 - bbox[1]),
        ascii_text,
        fill=(14, 14, 18),
        font=font,
    )
    # Hologram / registration sheen.
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    for i in range(0, width, 26):
        odraw.line([(i, 0), (i + 90, height)], fill=(120, 200, 255, 10), width=6)
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    return image


def render_scene(
    text: str,
    *,
    kind: str = "private",
    scene: str = "dusk",
    seed: int | None = None,
    with_vehicle: bool = True,
) -> bytes:
    """Composite a plate into a degraded, camera-like frame."""
    rng = random.Random(seed if seed is not None else hash(text) % 10_000)
    width, height = 1000, 620
    top, bottom = SCENES.get(scene, SCENES["dusk"])
    gradient = np.zeros((height, width, 3), dtype=np.float32)
    for y in range(height):
        ratio = y / max(1, height - 1)
        gradient[y, :, :] = np.array(top) * (1 - ratio) + np.array(bottom) * ratio
    if with_vehicle:
        body = np.array([rng.randint(24, 70), rng.randint(26, 74), rng.randint(34, 88)], dtype=np.float32)
        gradient[int(height * 0.42) :, int(width * 0.14) : int(width * 0.86)] = body * 0.85
    noise = np.random.default_rng(seed).normal(0, 6.5, gradient.shape)
    frame = np.clip(gradient + noise, 0, 255).astype(np.uint8)
    image = Image.fromarray(frame)

    plate = render_plate(text, kind=kind, width=rng.randint(380, 460))
    angle = rng.uniform(-6.5, 6.5)
    plate = plate.rotate(angle, resample=Image.BICUBIC, expand=True)
    # Perspective squash to mimic yaw.
    squash = rng.uniform(0.88, 1.0)
    plate = plate.resize((plate.width, max(20, int(plate.height * squash))), Image.BICUBIC)

    px = int(width * 0.32 + rng.uniform(-40, 40))
    py = int(height * 0.30 + rng.uniform(-20, 30))
    # Subtle shadow beneath the plate.
    shadow = Image.new("RGBA", plate.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [0, 0, plate.width, plate.height], radius=5, fill=(0, 0, 0, 120)
    )
    image.paste(Image.new("RGB", plate.size, (0, 0, 0)), (px + 5, py + 7),
                shadow.split()[-1])
    image.paste(plate, (px, py))
    image = image.filter(ImageFilter.GaussianBlur(radius=rng.uniform(0.4, 1.15)))
    # Sensor bloom around bright areas.
    if scene == "night":
        image = image.point(lambda v: min(255, int(v * 1.08)))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=rng.randint(74, 90))
    return buffer.getvalue()


def next_sample(seed: int | None = None) -> tuple[bytes, str]:
    index = (seed if seed is not None else random.randrange(len(PLATE_POOL))) % len(PLATE_POOL)
    text, _, kind = PLATE_POOL[index]
    scene = list(SCENES)[index % len(SCENES)]
    return render_scene(text, kind=kind, scene=scene, seed=index), scene


def sample_gallery(limit: int = 6) -> list[dict]:
    gallery: list[dict] = []
    for index in range(min(limit, len(PLATE_POOL))):
        text, country, kind = PLATE_POOL[index]
        scene = list(SCENES)[index % len(SCENES)]
        gallery.append(
            {
                "id": f"sample-{index + 1}",
                "plate": text,
                "country": country,
                "kind": kind,
                "scene": scene,
                "label": f"{scene.title()} · {kind.title()}",
                "generated_at": datetime.now(UTC).isoformat(),
            }
        )
    return gallery


def sample_bytes(sample_id: str) -> bytes:
    try:
        index = int(sample_id.split("-")[-1]) - 1
    except ValueError:
        index = 0
    index = max(0, min(len(PLATE_POOL) - 1, index))
    text, _, kind = PLATE_POOL[index]
    scene = list(SCENES)[index % len(SCENES)]
    return render_scene(text, kind=kind, scene=scene, seed=index)


def _skew(point: tuple[float, float], degrees: float) -> tuple[float, float]:
    rad = math.radians(degrees)
    return (point[0] * math.cos(rad) - point[1] * math.sin(rad),
            point[0] * math.sin(rad) + point[1] * math.cos(rad))
