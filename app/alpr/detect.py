"""Multi-strategy licence-plate localisation.

Three complementary detectors run over the frame and their proposals are fused
with non-maximum suppression:

1. ``morph``  – horizontal Sobel response + morphological closing. Strong for
   front-facing, well-lit captures (the classic ANPR workhorse).
2. ``mser``   – Maximally Stable Extremal Regions grouped into text lines.
   Recovers plates at odd angles and under uneven lighting.
3. ``contour``– Canny + polygon approximation for framed plates with visible
   borders, including perspective-skewed quads.

Every proposal is scored by a cheap "does this look like text?" verifier before
OCR, which keeps the character classifier's workload low on noisy frames.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np

from app.alpr import imaging
from app.alpr.types import Box, Candidate

try:  # pragma: no cover
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

MIN_ASPECT = 1.15
MAX_ASPECT = 7.2
MIN_HEIGHT_RATIO = 0.018  # of image height
MAX_HEIGHT_RATIO = 0.92


def detect(
    rgb: np.ndarray,
    gray: np.ndarray,
    *,
    max_candidates: int = 6,
) -> tuple[list[Candidate], dict[str, int]]:
    """Return ranked plate candidates and per-strategy proposal counts."""
    h, w = gray.shape[:2]
    proposals: list[Candidate] = []
    counts: dict[str, int] = {}

    morph = _morph_candidates(gray)
    counts["morph"] = len(morph)
    proposals.extend(morph)

    mser = _mser_candidates(gray)
    counts["mser"] = len(mser)
    proposals.extend(mser)

    contour = _contour_candidates(gray)
    counts["contour"] = len(contour)
    proposals.extend(contour)

    # Also try a vertically flipped pass for upside-down mounts.
    flipped = _morph_candidates(gray[::-1, ::-1])
    counts["flip"] = len(flipped)
    for cand in flipped:
        proposals.append(
            Candidate(
                box=Box(w - cand.box.x - cand.box.w, h - cand.box.y - cand.box.h, cand.box.w, cand.box.h),
                score=cand.score * 0.85,
                strategy="morph",
            )
        )

    valid: list[Candidate] = []
    for cand in proposals:
        box = cand.box.clip(w, h)
        if box.h < h * MIN_HEIGHT_RATIO or box.h > h * MAX_HEIGHT_RATIO:
            continue
        if box.w < 40 or box.h < 12:
            continue
        if not MIN_ASPECT <= box.aspect <= MAX_ASPECT:
            continue
        if box.area > 0.82 * w * h:
            continue
        cand.box = box
        valid.append(cand)

    enriched = _enrich(valid, w, h)
    ranked = _rank(enriched, gray)
    picked = _nms(ranked, iou_threshold=0.88, max_items=max_candidates)
    return picked, counts


def _enrich(candidates: list[Candidate], width: int, height: int) -> list[Candidate]:
    """Add padded and merged variants.

    Detectors routinely return a box that hugs the glyphs but clips a leading
    character or the plate frame. Because the OCR stage re-verifies every
    hypothesis end to end, it is cheap (and accurate) to offer a few nearby
    boxes and let the reader choose.
    """
    out: list[Candidate] = list(candidates)
    for cand in candidates:
        for ratio in (0.04, 0.09):
            padded = cand.box.pad(ratio, width, height)
            if MIN_ASPECT - 0.35 <= padded.aspect <= MAX_ASPECT + 0.6:
                out.append(
                    Candidate(box=padded, score=cand.score * 0.985, strategy=f"{cand.strategy}+pad")
                )
    for i, a in enumerate(candidates):
        for b in candidates[i + 1 :]:
            if _iou(a.box, b.box) < 0.35:
                continue
            union = Box(
                min(a.box.x, b.box.x),
                min(a.box.y, b.box.y),
                max(a.box.x2, b.box.x2) - min(a.box.x, b.box.x),
                max(a.box.y2, b.box.y2) - min(a.box.y, b.box.y),
            ).clip(width, height)
            if union.area == max(a.box.area, b.box.area):
                continue
            if not MIN_ASPECT - 0.5 <= union.aspect <= MAX_ASPECT + 0.8:
                continue
            out.append(Candidate(box=union, score=max(a.score, b.score) * 0.99, strategy="union"))
    return out


# --------------------------------------------------------------------- morph
def _morph_candidates(gray: np.ndarray) -> list[Candidate]:
    if cv2 is None:
        return _fallback_projection_candidates(gray)
    _h, w = gray.shape[:2]
    scale = 1.0
    work = gray
    if w > 1280:  # keep kernel geometry resolution-independent
        work, scale = imaging.resize_max(gray, 1280)
    grad = cv2.Sobel(work, cv2.CV_16S, 1, 0, ksize=3)
    grad = cv2.convertScaleAbs(grad)
    grad = cv2.GaussianBlur(grad, (3, 3), 0)
    _, binary = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    kernel_w = max(9, int(work.shape[1] * 0.022)) | 1
    close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, 3))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, close_kernel, iterations=2)
    closed = cv2.erode(closed, None, iterations=1)
    closed = cv2.dilate(closed, None, iterations=2)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out: list[Candidate] = []
    for contour in contours:
        rect = cv2.minAreaRect(contour)
        (_, _), (rw, rh), angle = rect
        if rw < 20 or rh < 6:
            continue
        if rw < rh:
            rw, rh, angle = rh, rw, angle + 90
        aspect = rw / max(rh, 1e-6)
        if not 1.2 <= aspect <= 8.0:
            continue
        box_pts = cv2.boxPoints(rect)
        xs, ys = box_pts[:, 0], box_pts[:, 1]
        box = Box(
            int(xs.min() / scale), int(ys.min() / scale),
            int((xs.max() - xs.min()) / scale), int((ys.max() - ys.min()) / scale),
        )
        quad = (box_pts / scale).tolist()
        score = 0.55 + 0.25 * min(1.0, aspect / 4.0)
        out.append(Candidate(box=box, score=score, strategy="morph", quad=quad))
    return out


def _fallback_projection_candidates(gray: np.ndarray) -> list[Candidate]:
    """Pure-NumPy fallback: find dense horizontal gradient bands."""
    grad = np.abs(np.diff(gray.astype(np.int16), axis=1, prepend=0)).astype(np.float32)
    h, w = grad.shape
    row_energy = grad.mean(axis=1)
    threshold = row_energy.mean() + 0.6 * row_energy.std()
    bands: list[tuple[int, int]] = []
    start: int | None = None
    for y, energy in enumerate(row_energy):
        if energy > threshold and start is None:
            start = y
        elif energy <= threshold and start is not None:
            if y - start > max(10, h * 0.02):
                bands.append((start, y))
            start = None
    if start is not None:
        bands.append((start, h))
    candidates: list[Candidate] = []
    for y0, y1 in bands:
        candidates.append(Candidate(box=Box(0, y0, w, y1 - y0), score=0.4, strategy="fallback"))
    return candidates


# ---------------------------------------------------------------------- mser
def _mser_candidates(gray: np.ndarray) -> list[Candidate]:
    if cv2 is None:
        return []
    h, w = gray.shape[:2]
    work = gray
    scale = 1.0
    if max(h, w) > 1100:
        work, scale = imaging.resize_max(gray, 1100)
    wh, ww = work.shape[:2]
    try:
        mser = cv2.MSER_create(delta=4, min_area=max(24, int(wh * ww * 0.00004)),
                              max_area=int(wh * ww * 0.012))
        mser.setMinAreaVariation(0.35)
        _regions, boxes = mser.detectRegions(work)
    except Exception:  # pragma: no cover - MSER can fail on degenerate input
        return []
    chars: list[Box] = []
    for x, y, bw, bh in boxes:
        if bh < 8 or bw < 3:
            continue
        aspect = bw / bh
        if not 0.08 <= aspect <= 1.1:
            continue
        if bh > wh * 0.35:
            continue
        chars.append(Box(int(x), int(y), int(bw), int(bh)))
    if not chars:
        return []
    lines = _group_lines(chars)
    out: list[Candidate] = []
    for line in lines:
        if len(line) < 4:
            continue
        x0 = min(c.x for c in line)
        x1 = max(c.x2 for c in line)
        y0 = min(c.y for c in line)
        y1 = max(c.y2 for c in line)
        box = Box(int(x0 / scale), int(y0 / scale), int((x1 - x0) / scale), int((y1 - y0) / scale))
        if not MIN_ASPECT <= box.aspect <= MAX_ASPECT:
            continue
        coverage = sum(c.area for c in line) / max(1, box.area)
        score = 0.5 + 0.3 * min(1.0, len(line) / 8.0) + 0.2 * min(1.0, coverage / 0.55)
        out.append(Candidate(box=box, score=score, strategy="mser"))
    return out


def _group_lines(chars: list[Box], similarity: float = 0.45) -> list[list[Box]]:
    ordered = sorted(chars, key=lambda b: b.cx)
    lines: list[list[Box]] = []
    for char in ordered:
        placed = False
        for line in lines:
            ref = np.median([c.h for c in line])
            ref_cy = np.median([c.cy for c in line])
            if abs(char.h - ref) / max(ref, 1) <= similarity and abs(char.cy - ref_cy) <= ref * 0.7:
                last_x = max(c.x2 for c in line)
                if char.x - last_x <= ref * 2.6:
                    line.append(char)
                    placed = True
                    break
        if not placed:
            lines.append([char])
    return lines


# ------------------------------------------------------------------- contour
def _contour_candidates(gray: np.ndarray) -> list[Candidate]:
    if cv2 is None:
        return []
    work, scale = imaging.resize_max(gray, 1200)
    blurred = cv2.bilateralFilter(work, 9, 75, 75)
    edges = cv2.Canny(blurred, 60, 180)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    out: list[Candidate] = []
    for contour in contours:
        if cv2.contourArea(contour) < 900:
            continue
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.03 * perimeter, True)
        if len(approx) != 4:
            continue
        quad = approx.reshape(4, 2).astype(np.float32)
        x, y, bw, bh = cv2.boundingRect(approx)
        if bh == 0:
            continue
        aspect = bw / bh
        if not MIN_ASPECT <= aspect <= MAX_ASPECT:
            continue
        fill = cv2.contourArea(contour) / max(1.0, bw * bh)
        if fill < 0.62:
            continue
        box = Box(int(x / scale), int(y / scale), int(bw / scale), int(bh / scale))
        score = 0.5 + 0.3 * fill + 0.2 * min(1.0, aspect / 4.0)
        out.append(Candidate(box=box, score=score, strategy="contour", quad=(quad / scale).tolist()))
    return out


# ------------------------------------------------------------- rank + filter
def _rank(candidates: list[Candidate], gray: np.ndarray) -> list[Candidate]:
    scored: list[Candidate] = []
    for cand in candidates:
        crop = gray[cand.box.y : cand.box.y2, cand.box.x : cand.box.x2]
        if crop.size == 0:
            continue
        text_score = _text_likeness(crop)
        if text_score <= 0:
            continue
        # Favour candidates occupying a sensible portion of the frame.
        area_ratio = cand.box.area / float(gray.shape[0] * gray.shape[1])
        size_bonus = min(1.0, area_ratio / 0.06) * 0.12
        # A tighter crop around the same plate reads better than an over-tall one.
        plate_aspect = cand.box.aspect
        if plate_aspect > 3.9:
            size_bonus -= 0.05
        combined = 0.62 * text_score + 0.26 * cand.score + size_bonus
        if combined < 0.38:
            continue
        scored.append(replace(cand, score=round(float(combined), 4)))
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored


def _text_likeness(crop: np.ndarray) -> float:
    """Heuristic verifier: does the crop look like a line of characters?"""
    if crop.size < 240 or crop.shape[0] < 10:
        return 0.0
    if cv2 is None:
        return 0.55
    _mask, info = imaging.best_ink_mask(crop, target_h=96)
    if float(info.get("count", 0)) < 3:
        return 0.0
    return float(np.clip(info.get("score", 0.0), 0.0, 1.0))


def _nms(candidates: list[Candidate], iou_threshold: float, max_items: int) -> list[Candidate]:
    kept: list[Candidate] = []
    for cand in candidates:
        if all(_iou(cand.box, other.box) < iou_threshold for other in kept):
            kept.append(cand)
            if len(kept) >= max_items:
                break
    return kept


def _iou(a: Box, b: Box) -> float:
    x0, y0 = max(a.x, b.x), max(a.y, b.y)
    x1, y1 = min(a.x2, b.x2), min(a.y2, b.y2)
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    union = a.area + b.area - inter
    return inter / union if union else 0.0
