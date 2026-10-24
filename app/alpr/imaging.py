"""Image decoding, normalisation and geometry helpers.

Everything here is NumPy + Pillow (+ OpenCV when available). OpenCV is used for
the heavy lifting but the module degrades to a pure-NumPy fallback so the API
still boots on constrained platforms.
"""
from __future__ import annotations

import contextlib
import io
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from app.core.errors import ValidationFailed

try:  # pragma: no cover - availability depends on the platform
    import cv2

    CV2_AVAILABLE = True
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore
    CV2_AVAILABLE = False

MAX_PIXELS = 40_000_000


def load_image(data: bytes) -> Image.Image:
    """Decode bytes into an RGB Pillow image, honouring EXIF orientation."""
    if not data:
        raise ValidationFailed("Empty image payload.", code="empty_image")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise ValidationFailed("Unsupported or corrupt image file.", code="invalid_image") from exc
    if image.width * image.height > MAX_PIXELS:
        raise ValidationFailed("Image resolution is too large.", code="image_too_large")
    if image.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", image.size, (0, 0, 0))
        converted = image.convert("RGBA")
        background.paste(converted, mask=converted.split()[-1])
        image = background
    else:
        image = image.convert("RGB")
    with contextlib.suppress(Exception):  # pragma: no cover - malformed EXIF
        image = ImageOps.exif_transpose(image)
    return image


def to_numpy(image: Image.Image) -> np.ndarray:
    return np.asarray(image, dtype=np.uint8)


def to_rgb(array: np.ndarray) -> np.ndarray:
    if array.ndim == 2:
        return np.stack([array] * 3, axis=-1)
    if array.shape[2] == 4:
        return array[:, :, :3]
    return array


def resize_max(array: np.ndarray, max_dim: int) -> tuple[np.ndarray, float]:
    """Downscale so the longest edge is ``max_dim``. Returns (image, scale)."""
    h, w = array.shape[:2]
    longest = max(h, w)
    if longest <= max_dim:
        return array, 1.0
    scale = max_dim / float(longest)
    new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
    if CV2_AVAILABLE:
        resized = cv2.resize(array, (new_w, new_h), interpolation=cv2.INTER_AREA)
    else:
        resized = np.asarray(Image.fromarray(array).resize((new_w, new_h), Image.BILINEAR))
    return resized, scale


def to_gray(array: np.ndarray) -> np.ndarray:
    if array.ndim == 2:
        return array
    rgb = array[:, :, :3].astype(np.float32)
    gray = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
    return np.clip(gray, 0, 255).astype(np.uint8)


def clahe(gray: np.ndarray, clip: float = 2.4, grid: int = 8) -> np.ndarray:
    if CV2_AVAILABLE:
        return cv2.createCLAHE(clipLimit=clip, tileGridSize=(grid, grid)).apply(gray)
    # Fallback: per-tile mean/std normalisation.
    h, w = gray.shape[:2]
    tile_h, tile_w = max(1, h // grid), max(1, w // grid)
    out = gray.astype(np.float32).copy()
    for ty in range(0, h, tile_h):
        for tx in range(0, w, tile_w):
            tile = out[ty : ty + tile_h, tx : tx + tile_w]
            if tile.size == 0:
                continue
            mean, std = float(tile.mean()), float(tile.std()) + 1e-6
            tile[:] = np.clip((tile - mean) * (28.0 / std) + 128.0, 0, 255)
    return out.astype(np.uint8)


def sharpen(gray: np.ndarray, amount: float = 1.0) -> np.ndarray:
    if not CV2_AVAILABLE:
        return gray
    blur = cv2.GaussianBlur(gray, (0, 0), 3)
    return cv2.addWeighted(gray, 1 + amount, blur, -amount, 0)


def warp_perspective(array: np.ndarray, quad: list[list[float]], size: tuple[int, int]) -> np.ndarray:
    """Rectify a quadrilateral to a fronto-parallel rectangle of ``size`` (w, h)."""
    w, h = size
    src = np.array(quad, dtype=np.float32)
    # Order points: top-left, top-right, bottom-right, bottom-left
    s = src.sum(axis=1)
    d = (src[:, 1] - src[:, 0]).reshape(-1)  # y - x : top-right is minimal
    ordered = np.array(
        [src[np.argmin(s)], src[np.argmin(d)], src[np.argmax(s)], src[np.argmax(d)]],
        dtype=np.float32,
    )
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
    if CV2_AVAILABLE:
        matrix = cv2.getPerspectiveTransform(ordered, dst)
        return cv2.warpPerspective(array, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return resize(array, (w, h))


def resize(array: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    w, h = size
    if CV2_AVAILABLE:
        return cv2.resize(array, (w, h), interpolation=cv2.INTER_AREA)
    return np.asarray(Image.fromarray(array).resize((w, h), Image.BILINEAR))


def rotate(array: np.ndarray, angle: float) -> np.ndarray:
    h, w = array.shape[:2]
    if CV2_AVAILABLE:
        matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        return cv2.warpAffine(array, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return array


def otsu_threshold(gray: np.ndarray) -> float:
    if CV2_AVAILABLE:
        value, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return float(value)
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 127.0
    omega = np.cumsum(hist) / total
    mu = np.cumsum(hist * np.arange(256)) / total
    mu_t = mu[-1]
    denom = omega * (1 - omega) + 1e-9
    sigma = (mu_t * omega - mu) ** 2 / denom
    return float(np.argmax(sigma))


def ensure_contrast(gray: np.ndarray) -> np.ndarray:
    if gray.size == 0:
        return gray
    low, high = float(np.percentile(gray, 2)), float(np.percentile(gray, 98))
    if high - low < 25:
        return gray
    scaled = (gray.astype(np.float32) - low) * (255.0 / (high - low))
    return np.clip(scaled, 0, 255).astype(np.uint8)


def encode_jpeg(array: np.ndarray, quality: int = 88) -> bytes:
    image = Image.fromarray(to_rgb(array))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buffer.getvalue()


def thumbnail(array: np.ndarray, max_dim: int = 640, quality: int = 82) -> bytes:
    small, _ = resize_max(array, max_dim)
    return encode_jpeg(small, quality=quality)


def average_hash(array: np.ndarray, size: int = 16) -> str:
    """64-bit perceptual hash (aHash-compatible signature) for dedup."""
    gray = to_gray(array)
    small = np.asarray(Image.fromarray(gray).resize((size, size), Image.BILINEAR), dtype=np.float32)
    mean = small.mean()
    bits = (small > mean).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:0{size * size // 4}x}"


def hamming(a: str, b: str) -> int:
    if len(a) != len(b):
        return 10**6
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def sharpness(gray: np.ndarray) -> float:
    """Variance of the Laplacian, normalised to a 0-1 signal."""
    if CV2_AVAILABLE:
        value = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    else:
        gx = np.diff(gray.astype(np.float32), axis=1, prepend=gray[:, :1])
        gy = np.diff(gray.astype(np.float32), axis=0, prepend=gray[:1, :])
        value = float(np.var(gx + gy))
    return float(min(1.0, value / 900.0))


def drawing_array(image: Image.Image) -> Any:  # pragma: no cover - helper
    return np.array(image)


# --------------------------------------------------------------- text masking
def _raw_components(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    try:
        import cv2

        count, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
        return [
            (int(stats[i, 0]), int(stats[i, 1]), int(stats[i, 2]), int(stats[i, 3]))
            for i in range(1, count)
        ]
    except Exception:  # pragma: no cover
        return []


def text_line_components(
    mask: np.ndarray, target_h: int
) -> tuple[list[tuple[int, int, int, int]], dict[str, float]]:
    """Pick the most text-like horizontal line of blobs in an ink mask.

    Works by clustering blobs with similar heights and vertical centres, then
    scoring each cluster on population, height consistency and aspect sanity.
    This is robust to the plate frame, the country band and dirt speckle, which
    all sit in different height bands than the characters themselves.
    """
    height, width = mask.shape[:2]
    comps = [
        (x, y, w, h)
        for (x, y, w, h) in _raw_components(mask)
        if w >= 2 and h >= 5 and w * h >= 14
        and x > 1 and y > 1 and x + w < width - 1 and y + h < height - 1
        and w < width * 0.45 and h < height * 0.99
    ]
    if not comps:
        return [], {"count": 0.0, "consistency": 0.0, "coverage": 0.0, "score": 0.0}

    best: list[tuple[int, int, int, int]] = []
    best_score = 0.0
    best_info: dict[str, float] = {"count": 0.0, "consistency": 0.0, "coverage": 0.0, "score": 0.0}
    for ref in comps:
        ref_h = ref[3]
        ref_cy = ref[1] + ref[3] / 2
        cluster = [
            c for c in comps
            if 0.68 <= c[3] / ref_h <= 1.45
            and abs((c[1] + c[3] / 2) - ref_cy) <= ref_h * 0.55
            and c[2] / max(1, c[3]) <= 1.35
        ]
        if len(cluster) < 3:
            continue
        heights = np.array([c[3] for c in cluster], dtype=np.float64)
        mean_h = float(heights.mean())
        consistency = 1.0 - min(1.0, float(heights.std()) / max(1.0, mean_h))
        widths = np.array([c[2] for c in cluster], dtype=np.float64)
        span = (max(c[0] + c[2] for c in cluster) - min(c[0] for c in cluster)) + 1
        fill = float(widths.sum()) / max(1.0, span)
        population = min(1.0, len(cluster) / 7.0)
        score = 0.46 * population + 0.34 * consistency + 0.20 * min(1.0, fill)
        if score > best_score:
            best, best_score = cluster, score
            best_info = {
                "count": float(len(cluster)),
                "consistency": round(consistency, 3),
                "coverage": round(fill, 3),
                "score": round(score, 3),
                "mean_height": round(mean_h, 1),
            }
    return sorted(best, key=lambda c: c[0]), best_info


def best_ink_mask(gray: np.ndarray, target_h: int = 96) -> tuple[np.ndarray, dict]:
    """Binarise a plate crop and pick the polarity that looks like text."""
    working = gray
    if working.shape[0] != target_h:
        scale = target_h / max(1, working.shape[0])
        working = resize(working, (max(12, int(working.shape[1] * scale)), target_h))
    equalised = clahe(working, clip=2.0, grid=6)
    threshold = otsu_threshold(equalised)
    candidates = {
        "dark-on-light": equalised <= threshold,
        "light-on-dark": equalised > threshold,
    }
    best_mask = candidates["dark-on-light"]
    best_info: dict = {"polarity": "dark-on-light", "score": 0.0, "count": 0.0}
    for name, raw in candidates.items():
        mask = raw
        try:
            import cv2

            mask = cv2.morphologyEx(
                mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((2, 2), np.uint8)
            ).astype(bool)
        except Exception:  # pragma: no cover
            pass
        _, info = text_line_components(mask, target_h)
        score = float(info.get("score", 0.0))
        coverage = float(mask.mean())
        if coverage > 0.72 or coverage < 0.03:
            score -= 0.35
        if score > float(best_info.get("score", 0.0)):
            best_mask, best_info = mask, {"polarity": name, "coverage": round(coverage, 3), **info}
    return best_mask, best_info
