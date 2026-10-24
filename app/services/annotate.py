"""Render detection overlays so operators can audit what the model saw."""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from app.alpr.types import RecognitionResult

ACCENT = (201, 184, 255)
DANGER = (255, 107, 107)
INK = (245, 245, 250)


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            from app.core.config import PROJECT_ROOT

            return ImageFont.truetype(str(PROJECT_ROOT / "app" / "assets" / "fonts" / name), size)
        except Exception:  # pragma: no cover
            continue
    return ImageFont.load_default()


def annotate(image: Image.Image, result: RecognitionResult) -> bytes:
    canvas = image.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas, "RGBA")
    width, height = canvas.size
    base = max(13, int(min(width, height) * 0.022))

    for index, plate in enumerate(result.plates):
        box = plate.box
        color = ACCENT if index == 0 else (150, 150, 170)
        thickness = max(2, int(min(width, height) * 0.0035))
        draw.rectangle(
            [box.x, box.y, box.x2, box.y2],
            outline=(*color, 255),
            width=thickness,
        )
        label = f" {plate.plate} · {plate.confidence * 100:.1f}% "
        font = _font(base)
        try:
            text_w = int(draw.textlength(label, font=font))
        except Exception:  # pragma: no cover
            text_w = len(label) * base // 2
        text_h = base + 8
        top = max(0, box.y - text_h)
        draw.rounded_rectangle(
            [box.x, top, min(width, box.x + text_w), top + text_h],
            radius=4,
            fill=(*color, 235),
        )
        draw.text((box.x + 4, top + 3), label, fill=(15, 12, 24, 255), font=font)

        for glyph_index, glyph in enumerate(plate.glyphs):
            g = glyph.box
            draw.rectangle(
                [g.x, g.y, g.x2, g.y2],
                outline=(255, 255, 255, 90),
                width=1,
            )
            if glyph_index == 0 and base > 12:
                draw.text((g.x, g.y2 + 2), glyph.label, fill=(*color, 255), font=_font(int(base * 0.8)))

    footer = f"{result.engine_version} · {result.processing_ms:.0f} ms · {len(result.plates)} plate(s)"
    font = _font(int(base * 0.85))
    padding = 10
    try:
        text_w = int(draw.textlength(footer, font=font))
    except Exception:  # pragma: no cover
        text_w = len(footer) * base // 2
    draw.rounded_rectangle(
        [padding, height - base - 2 * padding, padding + text_w + 2 * padding, height - padding],
        radius=6,
        fill=(10, 8, 16, 190),
    )
    draw.text((padding * 2, height - base - padding - 2), footer, fill=INK, font=font)

    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=88, optimize=True)
    return buffer.getvalue()
