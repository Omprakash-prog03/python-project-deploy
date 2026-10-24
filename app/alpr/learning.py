"""Correction-driven glyph learning.

Operators correct misreads every day; those corrections are the cheapest
training data an on-premise ALPR install will ever see. When a review
correction is confirmed, the glyph crops that produced the wrong string are
stored as exemplars for the *correct* character. The classifier folds them
into its correlation corpus, so the next frame from the same camera — same
typeface, same exposure, same motion blur — is recognised more reliably.

The store is a small ``.npz`` file next to the glyph-bank cache: bounded (60
exemplars per class), atomic to write, and disposable (delete it and the
reader falls back to the rendered bank).
"""
from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import numpy as np

from app.alpr.grammar import ALPHABET
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

VERSION = 1
MAX_PER_CLASS = 60
EMPTY = np.zeros((0, 48, 32), dtype=np.float32)

_lock = threading.RLock()
_cache: dict[str, object] = {"mtime": None, "labels": [], "bitmaps": EMPTY}


# ------------------------------------------------------------------- storage
def _path() -> Path:
    return Path(settings.model_cache_dir) / f"learned-glyphs-v{VERSION}.npz"


def load() -> tuple[list[str], np.ndarray]:
    """Return (labels, bitmaps). Cached until the file changes on disk."""
    path = _path()
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return [], EMPTY
    with _lock:
        if _cache["mtime"] == mtime and _cache["bitmaps"] is not None:
            return _cache["labels"], _cache["bitmaps"]  # type: ignore[return-value]
        try:
            data = np.load(path, allow_pickle=False)
            labels = [str(item) for item in data["labels"]]
            bitmaps = data["bitmaps"].astype(np.float32)
        except Exception:  # pragma: no cover - corrupt store
            logger.warning("Learned glyph store unreadable; ignoring it", exc_info=True)
            return [], EMPTY
        _cache.update(mtime=mtime, labels=labels, bitmaps=bitmaps)
        return labels, bitmaps


def _write(labels: list[str], bitmaps: np.ndarray) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.stem + ".tmp.npz")
    np.savez_compressed(temp, labels=np.array(labels), bitmaps=bitmaps.astype(np.float32))
    temp.replace(path)


def add(pairs: Iterable[tuple[str, np.ndarray]]) -> dict[str, int]:
    """Store exemplars for correctly-spelled characters."""
    useful: list[tuple[str, np.ndarray]] = []
    for label, bitmap in pairs:
        char = (label or "").upper()
        if char and char in ALPHABET and bitmap is not None and np.asarray(bitmap).size:
            useful.append((char, np.asarray(bitmap, dtype=np.float32)))
    if not useful:
        summary = _counts()
        summary["added"] = 0
        return summary

    with _lock:
        labels, bitmaps = load()
        labels = list(labels)
        chunks = [bitmaps] if len(bitmaps) else []
        counts = Counter(labels)
        added = 0
        for char, bitmap in useful:
            if counts[char] >= MAX_PER_CLASS:
                continue
            labels.append(char)
            chunks.append(bitmap[None, ...])
            counts[char] += 1
            added += 1
        if not added:
            summary = _counts()
            summary["added"] = 0
            return summary
        merged = np.concatenate(chunks, axis=0)
        _write(labels, merged)
        _cache.update(mtime=_path().stat().st_mtime_ns, labels=labels, bitmaps=merged)

    _refresh_classifier()
    summary = _counts()
    summary["added"] = added
    logger.info("Learned %d glyph exemplar(s); %d stored", added, summary["total"])
    return summary


def _counts() -> dict[str, int]:
    labels, _ = load()
    return {"total": len(labels), "classes": len(set(labels))}


def stats() -> dict[str, object]:
    labels, _ = load()
    counts = Counter(labels)
    return {
        "exemplars": len(labels),
        "classes": len(counts),
        "per_class": dict(sorted(counts.items())),
        "max_per_class": MAX_PER_CLASS,
        "version": VERSION,
        "store": str(_path()),
    }


def reset() -> dict[str, object]:
    """Forget every learned exemplar (admin action)."""
    path = _path()
    with _lock:
        if path.exists():
            path.unlink()
        _cache.update(mtime=None, labels=[], bitmaps=EMPTY)
    _refresh_classifier()
    logger.info("Learned glyph store cleared")
    return stats()


def _refresh_classifier() -> None:
    try:
        from app.alpr.ocr import get_classifier

        get_classifier().refresh()
    except Exception:  # pragma: no cover - classifier not built yet
        logger.debug("Classifier refresh skipped", exc_info=True)


# --------------------------------------------------------------- alignment
def align_pairs(predicted: str, corrected: str, glyphs: list) -> list[tuple[str, np.ndarray]]:
    """Pair glyphs with the corrected spelling using global alignment.

    Only substitutions are returned: a glyph that was already read correctly
    teaches nothing, and insertions/deletions have no crop to attach to.

    A length change means the reader lost or invented a whole glyph, so every
    crop is offset against the intended spelling; learning from that would
    teach the classifier nonsense. Such corrections are still applied to the
    record, they just do not train.
    """
    if len(predicted) != len(corrected):
        return []
    if not predicted or not corrected or not glyphs or len(predicted) != len(glyphs):
        return []
    n, m = len(predicted), len(corrected)
    cost = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        cost[i][0] = i
    for j in range(1, m + 1):
        cost[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            same = predicted[i - 1] == corrected[j - 1]
            cost[i][j] = min(
                cost[i - 1][j - 1] + (0 if same else 1),
                cost[i - 1][j] + 1,
                cost[i][j - 1] + 1,
            )
    pairs: list[tuple[str, np.ndarray]] = []
    i, j = n, m
    while i > 0 and j > 0:
        same = predicted[i - 1] == corrected[j - 1]
        if cost[i][j] == cost[i - 1][j - 1] + (0 if same else 1):
            if not same:
                bitmap = getattr(glyphs[i - 1], "bitmap", None)
                if bitmap is not None:
                    pairs.append((corrected[j - 1], bitmap))
            i, j = i - 1, j - 1
        elif cost[i][j] == cost[i - 1][j] + 1:
            i -= 1
        else:
            j -= 1
    # A plate that differs in most characters was probably a different vehicle,
    # not a misread one.
    if len(pairs) > max(2, len(corrected) // 3):
        return []
    return pairs
