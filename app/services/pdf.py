"""Dependency-free PDF report writer.

The deployment ships without a PDF library on purpose: reports are a couple of
text lines and one JPEG, and base-14 fonts plus a DCTDecode image XObject cover
that completely. Keeping it here means the Docker image stays small and the
report cannot drift from the evidence it describes.

Everything is written through :class:`PDFBuilder`, which owns the object table
and the cross-reference offsets; callers describe pages in page space
(1 unit = 1 pt, origin bottom-left).
"""
from __future__ import annotations

import io
import unicodedata
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageFont, ImageOps

PAGE_W = 595.0  # A4 at 72 dpi
PAGE_H = 842.0
MARGIN = 46.0

INK = (0.09, 0.09, 0.11)
MUTED = (0.42, 0.42, 0.45)
LINE = (0.84, 0.84, 0.86)
ACCENT = (0.85, 0.63, 0.23)
PAPER = (0.99, 0.98, 0.96)


def _esc(text: str) -> str:
    """Escape a string for a PDF literal, folding it to the embedded charset."""
    cleaned = _ascii(text)
    return cleaned.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")



# --------------------------------------------------------------- typography
# The report embeds its own subset fonts. Base-14 Helvetica was the tempting
# shortcut, but every viewer substitutes it with a different face — Chrome,
# Preview and Acrobat disagreed by up to 19 % on the same string — so
# right-aligned columns drifted and overlapped. A 20 kB subset removes the
# guesswork: the metrics in this module are read from the same bytes the PDF
# carries, so layout is identical everywhere.
FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "pdf"
FIRST_CHAR = 32
LAST_CHAR = 126

_FALLBACK = str.maketrans({
    "\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u00a0": " ",
    "\u00b7": ".",
})


@lru_cache(maxsize=2)
def _font(bold: bool) -> tuple[bytes, tuple[int, ...]]:
    """Return (truetype bytes, per-character advances in 1/1000 em)."""
    name = "report-bold.ttf" if bold else "report-regular.ttf"
    payload = (FONT_DIR / name).read_bytes()
    font = ImageFont.truetype(io.BytesIO(payload), 1000)
    widths = tuple(int(round(font.getlength(chr(code)))) for code in range(FIRST_CHAR, LAST_CHAR + 1))
    return payload, widths


def text_width(text: str, size: float, *, bold: bool = False, tracking: float = 0.0) -> float:
    """Advance width of ``text`` in points, from the embedded font itself."""
    widths = _font(bold)[1]
    total = 0.0
    for char in text:
        code = ord(char)
        total += widths[code - FIRST_CHAR] if FIRST_CHAR <= code <= LAST_CHAR else widths[0]
    return total * size / 1000.0 + tracking * len(text)


def _ascii(text: str) -> str:
    """Fold a string to what the subset can draw."""
    folded = unicodedata.normalize("NFKD", text.translate(_FALLBACK))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return "".join(ch if 32 <= ord(ch) <= 126 else "?" for ch in folded)

def _rgb(color: tuple[float, float, float]) -> str:
    return " ".join(f"{value:.3f}" for value in color)


class PDFBuilder:
    """Minimal PDF object table, page tree and content-stream writer."""

    def __init__(self) -> None:
        self._objects: dict[int, bytes] = {}
        self._next = 1
        self._catalog = self._reserve()
        self._pages_root = self._reserve()
        self._page_ids: list[int] = []
        self._fonts: dict[str, int] = {}

    # ------------------------------------------------------------- plumbing
    def _reserve(self) -> int:
        number = self._next
        self._next += 1
        return number

    def add(self, body: bytes) -> int:
        number = self._reserve()
        self._objects[number] = body
        return number

    def _font(self, bold: bool) -> tuple[str, int]:
        """Register an embedded TrueType subset (Flate-compressed FontFile2)."""
        name = "F2" if bold else "F1"
        if name in self._fonts:
            return name, self._fonts[name]

        payload, widths = _font(bold)
        face = ImageFont.truetype(io.BytesIO(payload), 1000)
        ascent, descent = face.getmetrics()
        cap_height = face.getbbox("H")[3]
        ink = face.getbbox("Hxygjpq")
        base = "DejaVuSans-Bold" if bold else "DejaVuSans"

        compressed = zlib.compress(payload, 9)
        font_file = self.add(
            f"<< /Length {len(compressed)} /Length1 {len(payload)} /Filter /FlateDecode >>\nstream\n".encode()
            + compressed
            + b"\nendstream"
        )
        descriptor = self.add(
            (
                f"<< /Type /FontDescriptor /FontName /{base} /Flags 32 "
                f"/FontBBox [{ink[0]} {ink[1] - 200} {ink[2]} {ink[3]}] /ItalicAngle 0 "
                f"/Ascent {ascent} /Descent {-descent} /CapHeight {cap_height} /StemV 80 "
                f"/FontFile2 {font_file} 0 R >>"
            ).encode()
        )
        width_array = " ".join(str(w) for w in widths)
        self._fonts[name] = self.add(
            (
                f"<< /Type /Font /Subtype /TrueType /BaseFont /{base} "
                f"/FirstChar {FIRST_CHAR} /LastChar {LAST_CHAR} "
                f"/Widths [{width_array}] /FontDescriptor {descriptor} 0 R "
                f"/Encoding /WinAnsiEncoding >>"
            ).encode()
        )
        return name, self._fonts[name]

    def add_jpeg(self, payload: bytes) -> tuple[int, int, int]:
        """Embed JPEG bytes; returns (object id, pixel width, pixel height)."""
        with Image.open(io.BytesIO(payload)) as image:
            image = image.convert("RGB")
            width, height = image.size
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=88, optimize=True)
        data = buffer.getvalue()
        obj = self.add(
            (
                f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
                f"/Length {len(data)} >>\nstream\n"
            ).encode()
            + data
            + b"\nendstream"
        )
        return obj, width, height

    def add_page(self, content: bytes, xobjects: dict[str, int]) -> None:
        stream = self.add(
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"
        )
        # Both weight faces are registered up front: the content stream may use
        # either, and a page that references a font missing from its resource
        # dictionary renders with a fallback and extracts as garbage.
        self._font(bold=False)
        self._font(bold=True)
        font_dict = " ".join(f"/{name} {obj} 0 R" for name, obj in self._fonts.items())
        xobject_dict = " ".join(f"/{name} {obj} 0 R" for name, obj in xobjects.items())
        resources = f"<< /Font << {font_dict} >> /XObject << {xobject_dict} >> >>"
        page = self.add(
            (
                f"<< /Type /Page /Parent {self._pages_root} 0 R "
                f"/MediaBox [0 0 {PAGE_W:.0f} {PAGE_H:.0f}] /Resources {resources} "
                f"/Contents {stream} 0 R >>"
            ).encode()
        )
        self._page_ids.append(page)

    def finish(self, *, title: str, author: str = "PlateVision") -> bytes:
        kids = " ".join(f"{page} 0 R" for page in self._page_ids)
        self._objects[self._pages_root] = (
            f"<< /Type /Pages /Kids [{kids}] /Count {len(self._page_ids)} >>".encode()
        )
        self._objects[self._catalog] = (
            f"<< /Type /Catalog /Pages {self._pages_root} 0 R >>".encode()
        )
        info = self.add(
            f"<< /Title ({_esc(title)}) /Author ({_esc(author)}) /Producer (PlateVision) >>".encode()
        )

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: dict[int, int] = {}
        for number in sorted(self._objects):
            offsets[number] = len(out)
            out += f"{number} 0 obj\n".encode() + self._objects[number] + b"\nendobj\n"
        xref_at = len(out)
        size = max(self._objects) + 1
        out += f"xref\n0 {size}\n".encode()
        out += b"0000000000 65535 f \n"
        for number in range(1, size):
            offset = offsets.get(number, 0)
            out += f"{offset:010d} 00000 n \n".encode()
        out += (
            f"trailer\n<< /Size {size} /Root {self._catalog} 0 R /Info {info} 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n"
        ).encode()
        return bytes(out)


# ------------------------------------------------------------------ drawing
class Canvas:
    """Very small content-stream DSL — enough for reports, nothing more."""

    def __init__(self) -> None:
        self._ops: list[str] = []

    def _text(self, x: float, y: float, text: str, size: float, *, bold: bool,
              color: tuple[float, float, float], tracking: float = 0.0) -> None:
        font = "F2" if bold else "F1"
        self._ops.append(f"BT {_rgb(color)} rg /{font} {size:.2f} Tf")
        self._ops.append(f"1 0 0 1 {x:.2f} {y:.2f} Tm")
        if tracking:
            self._ops.append(f"{tracking:.2f} Tc")
        self._ops.append(f"({_esc(text)}) Tj ET")

    def text(self, x: float, y: float, text: str, size: float = 10.0, *, bold: bool = False,
             color: tuple[float, float, float] = INK, tracking: float = 0.0) -> None:
        self._text(x, y, text, size, bold=bold, color=color, tracking=tracking)

    def right(self, x: float, y: float, text: str, size: float = 10.0, *,
              bold: bool = False, color: tuple[float, float, float] = INK,
              tracking: float = 0.0) -> None:
        width = text_width(text, size, bold=bold, tracking=tracking)
        self._text(x - width, y, text, size, bold=bold, color=color, tracking=tracking)

    def rule(self, x1: float, y: float, x2: float, *, color: tuple[float, float, float] = LINE,
             width: float = 0.6) -> None:
        self._ops.append(
            f"{_rgb(color)} RG {width:.2f} w {x1:.2f} {y:.2f} m {x2:.2f} {y:.2f} l S"
        )

    def rect(self, x: float, y: float, w: float, h: float, *,
             color: tuple[float, float, float] = PAPER) -> None:
        self._ops.append(f"{_rgb(color)} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")

    def image(self, name: str, x: float, y: float, w: float, h: float) -> None:
        self._ops.append(f"q {w:.2f} 0 0 {h:.2f} {x:.2f} {y:.2f} cm /{name} Do Q")

    def to_bytes(self) -> bytes:
        return ("\n".join(self._ops) + "\n").encode("latin-1", "replace")


# -------------------------------------------------------------------- data
@dataclass(slots=True)
class ReportEntry:
    plate: str
    display: str
    captured_at: datetime
    confidence: float
    status: str = "pending"
    source: str = "upload"
    camera: str | None = None
    region: str | None = None
    plate_type: str | None = None
    engine: str | None = None
    capture_id: str = ""
    risk_level: str | None = None
    notes: str | None = None
    image: bytes | None = None


def _stamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")


def _fit(payload: bytes, max_w: float, max_h: float) -> tuple[bytes, float, float] | None:
    """Re-encode to an upright JPEG that fits the box, keeping aspect ratio."""
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            ratio = min(max_w / image.width, max_h / image.height)
            width = max(1, int(image.width * ratio))
            height = max(1, int(image.height * ratio))
            resized = image.resize((width, height), Image.LANCZOS)
            buffer = io.BytesIO()
            resized.save(buffer, format="JPEG", quality=86, optimize=True)
            return buffer.getvalue(), float(width), float(height)
    except Exception:  # pragma: no cover - corrupt stored frame
        return None


def _header(canvas: Canvas, title: str, subtitle: str) -> float:
    canvas.text(MARGIN, PAGE_H - 62, "PLATEVISION", 9, tracking=2.4, color=MUTED)
    canvas.right(PAGE_W - MARGIN, PAGE_H - 62, title.upper(), 9, tracking=2.4, color=MUTED)
    canvas.text(MARGIN, PAGE_H - 116, subtitle, 26, bold=True)
    canvas.rule(MARGIN, PAGE_H - 134, PAGE_W - MARGIN)
    return PAGE_H - 134


def _clip(text: str, size: float, max_width: float) -> str:
    """Truncate to the widest prefix that fits, with an ellipsis."""
    if text_width(text, size) <= max_width:
        return text
    trimmed = text
    while trimmed and text_width(trimmed + "...", size) > max_width:
        trimmed = trimmed[:-1]
    return trimmed.rstrip() + "..."


def _footer(canvas: Canvas, page: int, pages: int, generated: datetime) -> None:
    canvas.rule(MARGIN, 54, PAGE_W - MARGIN)
    canvas.text(MARGIN, 40, f"Generated {_stamp(generated)} · PlateVision evidence record", 8,
                color=MUTED)
    canvas.right(PAGE_W - MARGIN, 40, f"PAGE {page} / {pages}", 8, color=MUTED)


def _fields(canvas: Canvas, label_x: float, value_x: float, y: float,
            rows: list[tuple[str, str]]) -> float:
    """Label left, value right-aligned in its column, one row per field.

    Right-aligning the values is what keeps long timestamps from colliding with
    the next column — the reason the first draft overlapped.
    """
    for label, value in rows:
        canvas.text(label_x, y, label.upper(), 7.5, tracking=1.2, color=MUTED)
        canvas.right(value_x, y, value, 10.5)
        y -= 20
    return y


# ------------------------------------------------------------------ reports
def single_report(entry: ReportEntry, *, generated: datetime | None = None) -> bytes:
    """One evidence sheet: the frame, the reading and the provenance."""
    generated = generated or datetime.now(UTC)
    doc = PDFBuilder()
    canvas = Canvas()
    xobjects: dict[str, int] = {}

    top = _header(canvas, "evidence report", "Plate recognition record")

    canvas.text(MARGIN, top - 66, entry.display or entry.plate, 40, bold=True)
    canvas.text(MARGIN, top - 92, entry.plate, 13, color=MUTED, tracking=1.6)

    image_top = top - 118
    box_h = 300.0
    if entry.image:
        fitted = _fit(entry.image, PAGE_W - 2 * MARGIN, box_h)
        if fitted:
            payload, width, height = fitted
            obj, _pw, _ph = doc.add_jpeg(payload)
            xobjects["Im1"] = obj
            x = MARGIN + (PAGE_W - 2 * MARGIN - width) / 2
            canvas.rect(MARGIN, image_top - box_h, PAGE_W - 2 * MARGIN, box_h, color=(0.96, 0.95, 0.93))
            canvas.image("Im1", x, image_top - (box_h + height) / 2, width, height)
    image_bottom = image_top - box_h

    y = image_bottom - 46
    rows_left = [
        ("captured", _stamp(entry.captured_at)),
        ("confidence", f"{entry.confidence * 100:.1f} %"),
        ("status", entry.status.upper()),
        ("source", (entry.source or "upload").upper()),
    ]
    rows_right = [
        ("camera", entry.camera or "Unassigned"),
        ("jurisdiction", entry.region or "—"),
        ("plate type", (entry.plate_type or "unknown").upper()),
        ("engine", entry.engine or "—"),
    ]
    left_end = _fields(canvas, MARGIN, MARGIN + 250, y, rows_left)
    right_end = _fields(canvas, PAGE_W / 2 + 24, PAGE_W - MARGIN, y, rows_right)
    y = min(left_end, right_end) - 12

    if entry.risk_level:
        canvas.text(MARGIN, y, "RISK LEVEL", 7.5, tracking=1.2, color=MUTED)
        canvas.right(PAGE_W - MARGIN, y, entry.risk_level.upper(), 10.5)
        y -= 20
    if entry.notes:
        canvas.text(MARGIN, y, "OPERATOR NOTE", 7.5, tracking=1.2, color=MUTED)
        canvas.right(PAGE_W - MARGIN, y, _clip(entry.notes, 9.5, PAGE_W - 2 * MARGIN - 120),
                     9.5, color=MUTED)
        y -= 20

    canvas.rule(MARGIN, 76, PAGE_W - MARGIN)
    canvas.text(MARGIN, 62, f"CAPTURE {entry.capture_id}", 7.5, color=MUTED, tracking=1.2)
    _footer(canvas, 1, 1, generated)

    doc.add_page(canvas.to_bytes(), xobjects)
    return doc.finish(title=f"PlateVision evidence — {entry.plate}")


def history_report(entries: list[ReportEntry], *, generated: datetime | None = None,
                   subtitle: str = "Scan history") -> bytes:
    """A contact sheet: every stored scan, newest first, with thumbnails."""
    generated = generated or datetime.now(UTC)
    doc = PDFBuilder()
    per_page = 7
    pages = [entries[i : i + per_page] for i in range(0, len(entries), per_page)] or [[]]

    for index, chunk in enumerate(pages):
        canvas = Canvas()
        xobjects: dict[str, int] = {}
        first = index * per_page + 1
        last = index * per_page + len(chunk)
        top = _header(canvas, f"scans {first}-{last}", subtitle)

        y = top - 34
        for offset, entry in enumerate(chunk):
            if entry.image:
                fitted = _fit(entry.image, 96, 58)
                if fitted:
                    payload, width, height = fitted
                    name = f"Im{offset + 1}"
                    obj, _pw, _ph = doc.add_jpeg(payload)
                    xobjects[name] = obj
                    canvas.image(name, MARGIN, y - 44 + (58 - height) / 2, width, height)

            text_x = MARGIN + 112
            right = PAGE_W - MARGIN
            canvas.text(text_x, y - 6, entry.display or entry.plate, 15, bold=True)
            canvas.right(right, y - 6, f"{entry.confidence * 100:.1f} %", 10)
            meta_line = " · ".join(
                part for part in (
                    _stamp(entry.captured_at),
                    (entry.status or "").upper(),
                    _clip(entry.camera or "Unassigned", 8, 140),
                ) if part
            )
            canvas.text(text_x, y - 24, meta_line, 8.5, color=MUTED)

            y -= 76
            canvas.rule(MARGIN, y + 18, PAGE_W - MARGIN)

        if not chunk:
            canvas.text(MARGIN, top - 60, "No scans recorded yet.", 12, color=MUTED)

        canvas.text(MARGIN, 76, f"{len(entries)} scan(s) in this export", 8, color=MUTED,
                    tracking=1.2)
        _footer(canvas, index + 1, len(pages), generated)
        doc.add_page(canvas.to_bytes(), xobjects)

    return doc.finish(title="PlateVision scan history")
