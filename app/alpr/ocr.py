"""Character segmentation and classification.

The classifier is a **hybrid**: a rendered-glyph template bank (normalised
cross-correlation) fused with a HOG k-NN vote, re-weighted by structural
descriptors (hole count, aspect, ink density). It needs no GPU, no model
download and no network access at runtime — which is exactly what fleet
operators want when the edge box sits in a toll plaza with flaky connectivity.

The bank is generated from the shipped DejaVu fonts on first boot, then cached
to ``storage/models`` so subsequent cold starts are instant.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.alpr import imaging, learning
from app.alpr.grammar import ALPHABET
from app.alpr.types import Box, Glyph
from app.core.config import PROJECT_ROOT
from app.core.logging import get_logger

logger = get_logger(__name__)

GLYPH_W, GLYPH_H = 32, 48
CANVAS = (GLYPH_W, GLYPH_H)
FONT_DIR = PROJECT_ROOT / "app" / "assets" / "fonts"
# A deliberately diverse bank: plate faces are condensed sans-serifs in the
# wild, so the classifier is trained on several letterform families.
FONT_FILES = (
    "DejaVuSans-Bold.ttf",
    "DejaVuSans.ttf",
    "RobotoCondensed-Bold.ttf",
    "BarlowCondensed-Bold.ttf",
    "Oswald-Bold.ttf",
    "Archivo-Bold.ttf",
)
TEMPLATE_VERSION = "v5"


# ------------------------------------------------------------------ features
def normalize_bitmap(bitmap: np.ndarray, size: tuple[int, int] = CANVAS, pad: int = 2) -> np.ndarray:
    """Center a tight ink crop on a fixed canvas, preserving aspect ratio."""
    ink = bitmap > 0.4
    if not ink.any():
        return np.zeros((size[1], size[0]), dtype=np.float32)
    ys, xs = np.where(ink)
    crop = bitmap[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    target_w, target_h = size[0] - 2 * pad, size[1] - 2 * pad
    h, w = crop.shape
    scale = min(target_w / max(w, 1), target_h / max(h, 1))
    new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
    resized = np.asarray(
        Image.fromarray((crop * 255).astype(np.uint8)).resize((new_w, new_h), Image.BILINEAR),
        dtype=np.float32,
    ) / 255.0
    canvas = np.zeros((size[1], size[0]), dtype=np.float32)
    y0 = (size[1] - new_h) // 2
    x0 = (size[0] - new_w) // 2
    canvas[y0 : y0 + new_h, x0 : x0 + new_w] = resized
    return canvas


def hole_count(bitmap: np.ndarray) -> int:
    """Count enclosed background regions (0,8,B -> 2; A,4 -> 1; 7 -> 0)."""
    ink = bitmap > 0.4
    try:
        import cv2

        contours, hierarchy = cv2.findContours(
            ink.astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE
        )
        if hierarchy is None:
            return 0
        holes = 0
        for idx, h in enumerate(hierarchy[0]):
            if h[3] != -1 and cv2.contourArea(contours[idx]) > 12:
                holes += 1
        return holes
    except Exception:  # pragma: no cover - fallback flood fill
        h, w = ink.shape
        visited = np.zeros_like(ink, dtype=bool)
        holes = 0
        from collections import deque

        for y in range(h):
            for x in range(w):
                if ink[y, x] or visited[y, x]:
                    continue
                queue = deque([(y, x)])
                visited[y, x] = True
                touches_border = False
                size = 0
                while queue:
                    cy, cx = queue.popleft()
                    size += 1
                    if cy in (0, h - 1) or cx in (0, w - 1):
                        touches_border = True
                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < h and 0 <= nx < w and not ink[ny, nx] and not visited[ny, nx]:
                            visited[ny, nx] = True
                            queue.append((ny, nx))
                if not touches_border and size > 10:
                    holes += 1
        return holes


def hog_features(bitmap: np.ndarray, cells: tuple[int, int] = (4, 6), bins: int = 8) -> np.ndarray:
    """Compact HOG descriptor (4x6 cells, 8 orientation bins, 2x2 blocks)."""
    gy, gx = np.gradient(bitmap.astype(np.float32))
    magnitude = np.hypot(gx, gy)
    angle = (np.arctan2(gy, gx) % np.pi) / np.pi * bins
    binary = (angle.astype(int) % bins).astype(np.int32)
    h, w = bitmap.shape
    ch, cw = h // cells[1], w // cells[0]
    descriptors: list[float] = []
    cell_hists = np.zeros((cells[1], cells[0], bins), dtype=np.float32)
    for cy in range(cells[1]):
        for cx in range(cells[0]):
            block_mag = magnitude[cy * ch : (cy + 1) * ch, cx * cw : (cx + 1) * cw]
            block_bin = binary[cy * ch : (cy + 1) * ch, cx * cw : (cx + 1) * cw]
            hist = np.bincount(block_bin.ravel(), weights=block_mag.ravel(), minlength=bins)
            cell_hists[cy, cx] = hist
    for cy in range(cells[1]):
        for cx in range(cells[0]):
            block = cell_hists[cy : cy + 2, cx : cx + 2].ravel() if (
                cy + 2 <= cells[1] and cx + 2 <= cells[0]
            ) else cell_hists[cy, cx]
            norm = float(np.linalg.norm(block)) + 1e-6
            descriptors.extend((block / norm).tolist())
    # Add coarse intensity + projection profiles.
    coarse = np.asarray(
        Image.fromarray((bitmap * 255).astype(np.uint8)).resize((6, 10), Image.BILINEAR),
        dtype=np.float32,
    ).ravel() / 255.0
    descriptors.extend(coarse.tolist())
    # Projection profiles carry shape information, but only if they are
    # normalised: raw ink sums swamp the histogram cells and make every
    # character look alike to the prototype k-NN.
    columns = bitmap.sum(axis=0, dtype=np.float32)
    rows = bitmap.sum(axis=1, dtype=np.float32)
    columns = columns / (float(columns.max()) + 1e-6)
    rows = rows / (float(rows.max()) + 1e-6)
    descriptors.extend(columns.tolist())
    descriptors.extend(rows.tolist())
    vector = np.asarray(descriptors, dtype=np.float32)
    return vector / (float(np.linalg.norm(vector)) + 1e-6)


def structural_descriptor(bitmap: np.ndarray) -> dict[str, float]:
    ink = bitmap > 0.4
    total = float(ink.sum())
    ys, xs = np.where(ink)
    if total == 0:
        return {"holes": 0.0, "aspect": 0.0, "fill": 0.0, "top_heavy": 0.0, "bottom_heavy": 0.0}
    height = ys.max() - ys.min() + 1
    width = xs.max() - xs.min() + 1
    return {
        "holes": float(hole_count(bitmap)),
        "aspect": float(width / max(1, height)),
        "fill": float(total / max(1, width * height)),
        "top_heavy": float(ink[: bitmap.shape[0] // 2].sum() / total),
        "bottom_heavy": float(ink[bitmap.shape[0] // 2 :].sum() / total),
    }


# --------------------------------------------------------------- template bank
class TemplateBank:
    """Rendered glyph bank with cached HOG descriptors."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir or PROJECT_ROOT / "storage" / "models"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.labels: list[str] = []
        self.bitmaps: np.ndarray = np.zeros((0, GLYPH_H, GLYPH_W), dtype=np.float32)
        self.features: np.ndarray = np.zeros((0, 1), dtype=np.float32)
        self.structures: list[dict[str, float]] = []
        self._ready = False

    # ------------------------------------------------------------------ build
    def ensure(self) -> None:
        if self._ready:
            return
        key = self._fingerprint()
        cache_file = self.cache_dir / f"glyphbank-{TEMPLATE_VERSION}-{key}.npz"
        if cache_file.exists():
            try:
                self._load(cache_file)
                self._ready = True
                return
            except Exception:  # pragma: no cover - corrupt cache
                logger.warning("Glyph cache unreadable, rebuilding.", exc_info=True)
        self._build()
        try:
            self._save(cache_file)
        except Exception:  # pragma: no cover
            logger.warning("Could not persist glyph cache", exc_info=True)
        self._ready = True

    def _fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(f"{GLYPH_W}x{GLYPH_H}".encode())
        for name in FONT_FILES:
            path = FONT_DIR / name
            if path.exists():
                digest.update(path.read_bytes()[:4096])
                digest.update(str(path.stat().st_size).encode())
        return digest.hexdigest()[:12]

    def _build(self) -> None:
        logger.info("Rendering glyph template bank (%d classes)", len(ALPHABET))
        labels: list[str] = []
        bitmaps: list[np.ndarray] = []
        for _font_index, font_name in enumerate(FONT_FILES):
            font_path = FONT_DIR / font_name
            if not font_path.exists():
                logger.warning("Font missing: %s", font_path)
                continue
            for font_size in (56, 76, 96):
                try:
                    font = ImageFont.truetype(str(font_path), font_size)
                except Exception:  # pragma: no cover
                    continue
                for char in ALPHABET:
                    for angle in (-5.0, 0.0, 5.0):
                        rendered = self._render(char, font, angle)
                        if rendered is None:
                            continue
                        norm = normalize_bitmap(rendered)
                        variants = [norm]
                        if angle == 0.0:
                            variants.extend(self._morph_variants(norm))
                        for variant in variants:
                            labels.append(char)
                            bitmaps.append(variant)
        self._finalize(labels, np.stack(bitmaps) if bitmaps else np.zeros((0, GLYPH_H, GLYPH_W)))

    @staticmethod
    def _render(char: str, font: ImageFont.FreeTypeFont, angle: float) -> np.ndarray | None:
        canvas = Image.new("L", (180, 180), 0)
        draw = ImageDraw.Draw(canvas)
        draw.text((30, 20), char, fill=255, font=font)
        bbox = canvas.getbbox()
        if bbox is None:
            return None
        canvas = canvas.crop(bbox)
        if angle:
            canvas = canvas.rotate(angle, resample=Image.BICUBIC, expand=True)
        array = np.asarray(canvas, dtype=np.float32) / 255.0
        return array

    @staticmethod
    def _morph_variants(bitmap: np.ndarray) -> list[np.ndarray]:
        """Light erosion/dilation to mimic print weight and sensor blur."""
        from PIL import ImageFilter

        image = Image.fromarray((bitmap * 255).astype(np.uint8))
        blurred = np.asarray(image.filter(ImageFilter.GaussianBlur(0.8)), dtype=np.float32) / 255.0
        dilated = np.clip(normalize_bitmap(blurred) * 1.35, 0, 1)
        eroded = np.clip(normalize_bitmap(blurred) ** 1.7, 0, 1)
        return [dilated.astype(np.float32), eroded.astype(np.float32)]

    def _finalize(self, labels: list[str], bitmaps: np.ndarray) -> None:
        if len(bitmaps) == 0:
            raise RuntimeError("Glyph template bank is empty; fonts are missing.")
        self.labels = labels
        self.bitmaps = bitmaps.astype(np.float32)
        self.features = np.stack([hog_features(b) for b in self.bitmaps]).astype(np.float32)
        self.structures = [structural_descriptor(b) for b in self.bitmaps]
        # Centre the descriptors once: cosine similarity on uncentred HOG
        # vectors is dominated by the shared ink mass (0.95+ for every class),
        # which hides the differences that actually separate characters.
        self.feature_mean = self.features.mean(axis=0)
        self.features = self.features - self.feature_mean
        # Centre + L2-normalise bitmaps for fast cosine correlation.
        flat = self.bitmaps.reshape(len(self.bitmaps), -1)
        flat = flat - flat.mean(axis=1, keepdims=True)
        self.corpus = flat / (np.linalg.norm(flat, axis=1, keepdims=True) + 1e-6)
        self.class_index = {
            c: np.array([i for i, label in enumerate(labels) if label == c]) for c in ALPHABET
        }
        self.class_prototypes = {
            c: np.mean(self.features[idx], axis=0) for c, idx in self.class_index.items() if len(idx)
        }
        logger.info("Glyph bank ready: %d templates, %d classes", len(labels), len(self.class_index))

    def _save(self, path: Path) -> None:
        np.savez_compressed(
            path,
            labels=np.array(self.labels),
            bitmaps=self.bitmaps,
            features=self.features,
        )

    def _load(self, path: Path) -> None:
        data = np.load(path, allow_pickle=False)
        labels = [str(x) for x in data["labels"]]
        bitmaps = data["bitmaps"].astype(np.float32)
        self._finalize(labels, bitmaps)
        logger.info("Glyph bank loaded from cache (%d templates)", len(labels))


# ------------------------------------------------------------------ classifier
class GlyphClassifier:
    def __init__(
        self,
        bank: TemplateBank | None = None,
        temperature: float = 0.055,
        template_weight: float = 0.58,
        knn_weight: float = 0.42,
        structure_weight: float = 1.0,
        learned_weight: float = 0.65,
    ) -> None:
        self.bank = bank or TemplateBank()
        self.temperature = temperature
        self.template_weight = template_weight
        self.knn_weight = knn_weight
        self.structure_weight = structure_weight
        self.learned_weight = learned_weight
        self._proto_matrix: np.ndarray | None = None
        self._corpus: np.ndarray | None = None
        self._class_index: dict[str, np.ndarray] | None = None
        self._learned: np.ndarray | None = None
        self._learned_labels: list[str] = []

    def _ensure(self) -> None:
        self.bank.ensure()
        if self._corpus is None or self._class_index is None:
            self._corpus, self._class_index = self._build_corpus()
            self._learned, self._learned_labels = self._build_learned()
        if self._proto_matrix is None:
            self._proto_matrix = np.stack([self.bank.class_prototypes[c] for c in ALPHABET])
            norms = np.linalg.norm(self._proto_matrix, axis=1, keepdims=True) + 1e-6
            self._proto_matrix = self._proto_matrix / norms

    def _build_learned(self) -> tuple[np.ndarray | None, list[str]]:
        """Verified exemplars, scored separately so one correction is not diluted."""
        labels, bitmaps = learning.load()
        if not labels:
            return None, []
        flat = bitmaps.reshape(len(bitmaps), -1).astype(np.float32)
        flat = flat - flat.mean(axis=1, keepdims=True)
        return flat / (np.linalg.norm(flat, axis=1, keepdims=True) + 1e-6), list(labels)

    def _build_corpus(self) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        """Rendered templates plus any glyphs learned from operator corrections."""
        labels, bitmaps = learning.load()
        if not labels:
            return self.bank.corpus, self.bank.class_index
        flat = bitmaps.reshape(len(bitmaps), -1).astype(np.float32)
        flat = flat - flat.mean(axis=1, keepdims=True)
        extra = flat / (np.linalg.norm(flat, axis=1, keepdims=True) + 1e-6)
        offset = len(self.bank.corpus)
        index = {char: idx.copy() for char, idx in self.bank.class_index.items()}
        for position, label in enumerate(labels):
            index.setdefault(label, np.zeros(0, dtype=np.int64))
            index[label] = np.append(index[label], offset + position)
        return np.vstack([self.bank.corpus, extra]), index

    def refresh(self) -> None:
        """Rebuild the merged corpus after the learned store changes."""
        self._corpus = None
        self._class_index = None
        self._learned = None
        self._learned_labels = []
        self._ensure()

    def classify(self, bitmap: np.ndarray) -> dict[str, float]:
        self._ensure()
        bitmap = normalize_bitmap(bitmap)
        flat = bitmap.reshape(-1)
        centered = flat - flat.mean()
        norm = np.linalg.norm(centered) + 1e-6
        query = centered / norm
        assert self._corpus is not None and self._class_index is not None
        correlation = self._corpus @ query  # [-1, 1]
        # Template score per class: mean of the top-4 matches.
        template_scores: dict[str, float] = {}
        for char, idx in self._class_index.items():
            if len(idx) == 0:
                continue
            top = np.sort(correlation[idx])[-4:]
            template_scores[char] = float(top.mean())

        learned_map: dict[str, float] = {}
        if self._learned is not None:
            similarities = self._learned @ query
            grouped: dict[str, list[float]] = {}
            for label, score in zip(self._learned_labels, similarities, strict=False):
                grouped.setdefault(label, []).append(float(score))
            learned_map = {char: max(sorted(v)[-2:]) for char, v in grouped.items()}

        features = hog_features(bitmap) - self.bank.feature_mean
        assert self._proto_matrix is not None
        feature_norm = features / (np.linalg.norm(features) + 1e-6)
        knn_scores = self._proto_matrix @ feature_norm
        knn_map = {char: float(knn_scores[i]) for i, char in enumerate(ALPHABET)}

        structure = structural_descriptor(bitmap)
        logits: dict[str, float] = {}
        for char in ALPHABET:
            if char not in template_scores:
                continue
            t = template_scores[char]
            # A verified correction outranks the rendered bank for this site:
            # same typeface, exposure and lens as the frames it came from.
            evidence = learned_map.get(char)
            if evidence is not None and evidence > t:
                t += self.learned_weight * (evidence - t)
            k = knn_map.get(char, 0.0)
            logit = self.template_weight * t + self.knn_weight * k
            logit += self.structure_weight * self._structural_bonus(
                char, structure, self.bank.structures
            )
            logits[char] = logit
        if not logits:
            return {c: 1.0 / len(ALPHABET) for c in ALPHABET}
        values = np.array(list(logits.values()), dtype=np.float64)
        values = (values - values.max()) / self.temperature
        probs = np.exp(np.clip(values, -60, 0))
        probs /= probs.sum()
        return {char: float(p) for char, p in zip(logits.keys(), probs)}

    @staticmethod
    def _structural_bonus(char: str, query: dict[str, float],
                          templates: list[dict[str, float]]) -> float:
        """Penalise characters whose hole-count/aspect disagree with the glyph."""
        expected_holes = {"0": 1, "8": 2, "6": 1, "9": 1, "4": 1, "A": 1, "D": 1, "O": 1,
                          "P": 1, "Q": 1, "B": 2, "R": 1, "G": 0, "g": 1}
        holes = int(query["holes"])
        target = expected_holes.get(char, 0)
        if holes == target:
            bonus = 0.09
        elif abs(holes - target) == 1:
            bonus = -0.05
        else:
            bonus = -0.22
        if char == "1" and query["aspect"] < 0.35:
            bonus += 0.08
        if char in "MW" and query["aspect"] < 0.62:
            bonus -= 0.12
        if char in "IL" and query["aspect"] > 0.62:
            bonus -= 0.12
        return bonus


# ---------------------------------------------------------------- segmentation
def _line_filter(line: list[tuple[int, int, int, int]], target_h: int) -> dict[str, object]:
    """Trim a raw text line down to character-sized blobs."""
    if not line:
        return {"components": 0, "reason": "empty"}
    heights = np.array([c[3] for c in line], dtype=np.float64)
    widths = np.array([c[2] for c in line], dtype=np.float64)
    areas = widths * heights
    median_h = float(np.median(heights))
    median_w = float(np.median(widths)) or 1.0
    median_area = float(np.median(areas)) or 1.0
    diagnostics: dict[str, object] = {
        "components": len(line),
        "median_height": round(median_h, 1),
        "target_height": target_h,
    }

    # Rows -> pick the most populated height band.
    band = [
        c for c in line
        if 0.72 * median_h <= c[3] <= 1.36 * median_h
    ]
    if len(band) >= 3 and len(band) < len(line):
        diagnostics["heights_trimmed"] = len(line) - len(band)
        line = band
        heights = np.array([c[3] for c in line], dtype=np.float64)
        median_h = float(np.median(heights))
        median_area = float(np.median([c[2] * c[3] for c in line])) or 1.0

    kept: list[tuple[int, int, int, int]] = []
    for index, comp in enumerate(line):
        x, _y, w, h = comp
        neighbours = [c for j, c in enumerate(line) if j != index]
        gap = min(
            (abs((c[0] + c[2] / 2) - (x + w / 2)) for c in neighbours), default=0.0
        )
        area = w * h
        # Contracted fragment marooned by a wide gap: 'I' seen as a sliver.
        if area < 0.55 * median_area and gap > 0.55 * median_w and w > h * 1.6:
            continue
        if w <= 1:
            continue
        # Very wide blobs are usually two characters fused by blur. Keep them,
        # but flag them so the classifier can attempt a split.
        kept.append(comp)
    if len(kept) < 3:
        kept = list(line)
    diagnostics["glyph_count"] = len(kept)
    return {"line": kept, "diagnostics": diagnostics}


def _line_skew(line: list[tuple[int, int, int, int]]) -> float:
    """Estimate the text-line angle in degrees from blob centres.

    Handheld and roadside frames are rarely level; even three degrees of skew
    smears the components enough to fuse neighbours. The fit is only trusted
    when the blobs actually sit on a straight line and the tilt is worth
    correcting.
    """
    if len(line) < 5:
        return 0.0
    heights = np.array([c[3] for c in line], dtype=np.float64)
    centres_x = np.array([c[0] + c[2] / 2 for c in line], dtype=np.float64)
    centres_y = np.array([c[1] + c[3] / 2 for c in line], dtype=np.float64)
    slope, intercept = np.polyfit(centres_x, centres_y, 1)
    residual = np.abs(centres_y - (slope * centres_x + intercept))
    if float(residual.max()) > 0.45 * float(np.median(heights)):
        return 0.0
    angle = float(np.degrees(np.arctan(slope)))
    return angle if abs(angle) >= 2.0 else 0.0


def _rotate(gray: np.ndarray, angle: float) -> np.ndarray:
    image = Image.fromarray(gray.astype(np.uint8))
    fill = int(np.median(gray))
    return np.asarray(
        image.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=fill), dtype=gray.dtype
    )


def segment_glyphs(
    plate_gray: np.ndarray,
    *,
    trim: float = 0.02,
) -> tuple[list[Glyph], dict[str, object]]:
    """Cut a rectified plate crop into normalised character glyphs."""
    diagnostics: dict[str, object] = {"rows": 0, "components": 0, "polarity": None}
    if plate_gray.size == 0:
        return [], diagnostics

    bh, bw = plate_gray.shape[:2]
    if bh < 16 or bw < 24:
        return [], diagnostics
    margin_y = int(bh * trim)
    margin_x = int(bw * trim * 0.45)
    core = plate_gray[margin_y : bh - margin_y, margin_x : bw - margin_x]
    if core.size == 0:
        core, margin_y, margin_x = plate_gray, 0, 0

    target_h = 96
    scale = target_h / core.shape[0]
    work = imaging.resize(core, (max(24, int(core.shape[1] * scale)), target_h))
    ink, mask_info = imaging.best_ink_mask(work, target_h=target_h)
    diagnostics["polarity"] = mask_info.get("polarity")
    diagnostics["mask_score"] = mask_info.get("score")

    line, line_info = imaging.text_line_components(ink, target_h)
    skew = _line_skew(line)
    if skew:
        deskewed = _rotate(work, skew)
        ink, mask_info = imaging.best_ink_mask(deskewed, target_h=target_h)
        line, line_info = imaging.text_line_components(ink, target_h)
        diagnostics["skew_deg"] = round(skew, 2)
        diagnostics["mask_score"] = mask_info.get("score")
    diagnostics["components"] = len(line)
    if len(line) < 3:
        return [], diagnostics

    filtered = _line_filter(line, target_h)
    kept = filtered.get("line", [])  # type: ignore[assignment]
    diagnostics.update(filtered.get("diagnostics", {}))  # type: ignore[arg-type]
    if not kept:
        return [], diagnostics
    diagnostics["rows"] = 1

    inv = 1.0 / scale
    glyphs: list[Glyph] = []
    for (x, y, w, h) in kept:  # type: ignore[misc]
        crop = ink[y : y + h, x : x + w]
        if crop.size == 0:
            continue
        bitmap = normalize_bitmap(crop.astype(np.float32))
        glyphs.append(
            Glyph(
                bitmap=bitmap,
                box=Box(
                    int(x * inv + margin_x),
                    int(y * inv + margin_y),
                    max(1, int(w * inv)),
                    max(1, int(h * inv)),
                ),
                features=structural_descriptor(bitmap),
            )
        )
    glyphs = _merge_fragments(glyphs)
    diagnostics["glyph_count"] = len(glyphs)
    diagnostics["line"] = {k: v for k, v in line_info.items() if k != "score"}
    return glyphs, diagnostics


def _components(ink: np.ndarray) -> list[tuple[int, int, int, int]]:
    try:
        import cv2

        count, _, stats, _ = cv2.connectedComponentsWithStats(
            ink.astype(np.uint8), connectivity=8
        )
        return [
            (int(stats[i, 0]), int(stats[i, 1]), int(stats[i, 2]), int(stats[i, 3]))
            for i in range(1, count)
        ]
    except Exception:  # pragma: no cover
        from collections import deque

        h, w = ink.shape
        seen = np.zeros_like(ink, dtype=bool)
        boxes: list[tuple[int, int, int, int]] = []
        for y in range(h):
            for x in range(w):
                if not ink[y, x] or seen[y, x]:
                    continue
                queue = deque([(y, x)])
                seen[y, x] = True
                min_y = max_y = y
                min_x = max_x = x
                while queue:
                    cy, cx = queue.popleft()
                    min_y, max_y = min(min_y, cy), max(max_y, cy)
                    min_x, max_x = min(min_x, cx), max(max_x, cx)
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            ny, nx = cy + dy, cx + dx
                            if 0 <= ny < h and 0 <= nx < w and ink[ny, nx] and not seen[ny, nx]:
                                seen[ny, nx] = True
                                queue.append((ny, nx))
                boxes.append((min_x, min_y, max_x - min_x + 1, max_y - min_y + 1))
        return boxes


def _group_rows(boxes: list[Box], work_h: int) -> list[list[Box]]:
    rows: list[list[Box]] = []
    for box in sorted(boxes, key=lambda b: b.cy):
        placed = False
        for row in rows:
            ref_h = float(np.median([b.h for b in row]))
            ref_cy = float(np.median([b.cy for b in row]))
            if abs(box.cy - ref_cy) <= ref_h * 0.55 and 0.55 <= box.h / max(ref_h, 1) <= 1.85:
                row.append(box)
                placed = True
                break
        if not placed:
            rows.append([box])
    # Drop rows that are far shorter than the tallest (noise / frame remnants).
    if len(rows) > 1:
        tallest = max(sum(b.area for b in r) for r in rows)
        rows = [r for r in rows if sum(b.area for b in r) > tallest * 0.12]
    return rows


def _merge_fragments(glyphs: list[Glyph]) -> list[Glyph]:
    """Merge only true fragments (a dot over a stem), never adjacent glyphs.

    Characters on a plate are tightly spaced — a 1-2 px gap is *normal*, so any
    horizontal-gap heuristic silently fuses 'KA' into one blob. Merging is
    therefore restricted to boxes that overlap horizontally and differ sharply
    in height.
    """
    if len(glyphs) < 2:
        return glyphs
    merged: list[Glyph] = []
    for glyph in glyphs:
        if merged:
            prev = merged[-1]
            overlap = min(prev.box.x2, glyph.box.x2) - max(prev.box.x, glyph.box.x)
            shorter = min(prev.box.h, glyph.box.h)
            taller = max(prev.box.h, glyph.box.h)
            if overlap > 0 and shorter <= taller * 0.45:
                union = Box(
                    min(prev.box.x, glyph.box.x),
                    min(prev.box.y, glyph.box.y),
                    max(prev.box.x2, glyph.box.x2) - min(prev.box.x, glyph.box.x),
                    max(prev.box.y2, glyph.box.y2) - min(prev.box.y, glyph.box.y),
                )
                combined = np.maximum(prev.bitmap, glyph.bitmap)
                bitmap = normalize_bitmap(combined)
                merged[-1] = Glyph(bitmap=bitmap, box=union,
                                   features=structural_descriptor(bitmap))
                continue
        merged.append(glyph)
    return merged


@lru_cache(maxsize=1)
def get_classifier() -> GlyphClassifier:
    return GlyphClassifier()
