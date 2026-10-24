"""Licence-plate grammars, grammar-constrained decoding and validation.

OCR output is not taken at face value. Each plate belongs to a jurisdiction with
a known character pattern (e.g. Indian plates are ``SS RR L NNNN``). We run a
small edit-distance dynamic program that jointly aligns the segmented glyphs to
every candidate pattern and picks the most probable string. This is what turns
``MH12AB3456`` -> ``MH12AB3456`` instead of ``MHI2AB3456`` and lifts end-to-end
accuracy by a large margin on ambiguous glyphs (O/0, I/1, B/8, S/5, Z/2).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
DIGITS = "0123456789"
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
MIXED = ALPHABET

# Characters that look alike when a plate is compressed / motion blurred.
CONFUSION_GROUPS: tuple[str, ...] = (
    "0OQD8B6G",
    "1ILT7J",
    "2Z",
    "5S",
    "8B3",
    "4A",
    "9GQ",
    "MNHW",
    "UVY",
    "CEXK",
    "RKP",
    "FG",
    "PBR",
    "Z72",
    "6G9",
)

CONFUSION_PAIRS: dict[str, set[str]] = {}
for group in CONFUSION_GROUPS:
    for char in group:
        CONFUSION_PAIRS.setdefault(char, set()).update(set(group) - {char})


@dataclass(frozen=True)
class Token:
    kind: str  # 'A' alpha, 'D' digit, 'M' mixed
    minimum: int
    maximum: int

    @property
    def charset(self) -> str:
        return {"A": LETTERS, "D": DIGITS}.get(self.kind, MIXED)


@dataclass(frozen=True)
class Format:
    name: str
    country: str
    tokens: tuple[Token, ...]
    description: str = ""
    plate_type: str = "private"
    separator: str = ""
    weight: float = 1.0

    @property
    def min_len(self) -> int:
        return sum(t.minimum for t in self.tokens)

    @property
    def max_len(self) -> int:
        return sum(t.maximum for t in self.tokens)


def _fmt(name: str, country: str, spec: str, **kwargs) -> Format:
    """Build a Format from a compact spec such as ``A2 D2 A1-3 D4``."""
    tokens: list[Token] = []
    for part in spec.split():
        kind = part[0]
        counts = part[1:]
        if "-" in counts:
            lo, hi = counts.split("-")
            minimum, maximum = int(lo), int(hi)
        else:
            minimum = maximum = int(counts or 1)
        tokens.append(Token(kind=kind, minimum=minimum, maximum=maximum))
    return Format(name=name, country=country, tokens=tuple(tokens), **kwargs)


FORMATS: tuple[Format, ...] = (
    # --------------------------------------------------------------- India
    _fmt("IN_NEW", "IN", "A2 D1-2 A1-3 D4", description="High-security (HSRP) private vehicle"),
    _fmt("IN_STANDARD", "IN", "A2 D2 A2 D4", separator="-", description="Standard private vehicle"),
    _fmt("IN_COMMERCIAL", "IN", "A2 D2 A2 D4", plate_type="commercial", weight=0.9,
         description="Yellow commercial plate"),
    _fmt("IN_BH", "IN", "D2 A2 D4 A0-2", description="Bharat series (BH)"),
    _fmt("IN_DEFENCE", "IN", "D2 A1-2 D4-6 A0-1", plate_type="government", weight=0.7,
         description="Defence / government"),
    # ------------------------------------------------------------------ US
    _fmt("US_STANDARD", "US", "A1-3 D0-4 A0-3", weight=0.75, description="State plate, 5-8 chars"),
    # ------------------------------------------------------------------ UK
    _fmt("UK_STANDARD", "UK", "A2 D2 A3", description="Current UK series"),
    _fmt("UK_LEGACY", "UK", "A1-2 D1-4 A0-3", weight=0.5, description="Legacy UK series"),
    # ------------------------------------------------------------------ EU
    _fmt("EU_STANDARD", "EU", "A1-3 D1-4 A0-3", weight=0.85, description="EU national plate"),
    _fmt("DE_STANDARD", "EU", "A1-3 A1-2 D1-4", weight=0.5, description="Germany"),
    # ------------------------------------------------------------- fallback
    _fmt("GENERIC_ALNUM", "GENERIC", "M4-9", weight=0.35, description="Unconstrained alphanumeric"),
    _fmt("GENERIC_DIGITS", "GENERIC", "D4-8", weight=0.3, description="Numeric-only plate"),
)

FORMATS_BY_NAME = {f.name: f for f in FORMATS}


def formats_for(country: str | None) -> list[Format]:
    if not country:
        return list(FORMATS)
    country = country.upper()
    selected = [f for f in FORMATS if f.country == country]
    if country not in ("GENERIC",):
        selected.extend(f for f in FORMATS if f.country == "GENERIC")
    return selected or list(FORMATS)


def normalize(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _expand_counts(tokens: tuple[Token, ...], index: int, remaining: int, prefix: list[str]):
    """Yield every positional charset-kind sequence of the requested length.

    Each yielded value is a list of ``'A'``/``'D'``/``'M'`` markers, one per
    plate character position.
    """
    if index == len(tokens):
        if remaining == 0:
            yield list(prefix)
        return
    token = tokens[index]
    rest_min = sum(t.minimum for t in tokens[index + 1 :])
    rest_max = sum(t.maximum for t in tokens[index + 1 :])
    for count in range(token.minimum, token.maximum + 1):
        if remaining - count < rest_min or remaining - count > rest_max:
            continue
        yield from _expand_counts(
            tokens, index + 1, remaining - count, prefix + [token.kind] * count
        )


def _kind_of(marker: str) -> str:
    return {"A": "L", "D": "D"}.get(marker, "M")


@dataclass
class Decoded:
    text: str
    score: float
    format: Format
    probabilities: list[float] = field(default_factory=list)
    alignment: list[int] = field(default_factory=list)
    valid: bool = True

    @property
    def mean_probability(self) -> float:
        return float(sum(self.probabilities) / len(self.probabilities)) if self.probabilities else 0.0


def _smoothed(
    probabilities: dict[str, float], charset: str, epsilon: float = 0.22
) -> dict[str, float]:
    """Restrict a glyph distribution to a charset, then temper it.

    Raw probabilities are renormalised inside the charset (what is the chance
    this is *this* letter?) and then mixed with a uniform floor so that a
    genuinely impossible character still has a finite cost. Without the
    epsilon mix, every glyph would cost 0 for its argmax class and the edit
    distance would only measure gaps.
    """
    base: dict[str, float] = {}
    for char in charset:
        value = probabilities.get(char, 0.0)
        if value <= 0.0:
            friends = CONFUSION_PAIRS.get(char, set())
            value = max((probabilities.get(other, 0.0) for other in friends), default=0.0) * 0.16
        base[char] = value
    total = sum(base.values())
    if total <= 0:
        return {c: 1.0 / len(charset) for c in charset}
    floor = epsilon / len(charset)
    return {c: (1.0 - epsilon) * (v / total) + floor for c, v in base.items()}


SKIP_GLYPH_COST = 1.45   # a blob that is not part of the plate text
SKIP_CHAR_COST = 1.70    # a plate character the segmenter missed
KINDS = ("L", "D", "M")


def glyph_probability_table(glyph_probabilities: list[dict[str, float]]) -> list[dict[str, tuple[str, float]]]:
    """For each glyph, the best character and score under each charset kind."""
    kinds = {"L": LETTERS, "D": DIGITS, "M": MIXED}
    table: list[dict[str, tuple[str, float]]] = []
    for probs in glyph_probabilities:
        row: dict[str, tuple[str, float]] = {}
        for kind, charset in kinds.items():
            smoothed = _smoothed(probs, charset)
            char = max(smoothed, key=smoothed.get)  # type: ignore[arg-type]
            row[kind] = (char, smoothed[char])
        table.append(row)
    return table


def _align(
    table: list[dict[str, tuple[str, float]]], kinds: list[str]
) -> tuple[float, str, list[float], list[int]]:
    """Edit-distance alignment of glyphs to a positional charset pattern."""
    n, m = len(table), len(kinds)
    if n == 0 or m == 0:
        return math.inf, "", [], []
    infinity = math.inf
    dp = [[infinity] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int, str] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(n + 1):
        row = dp[i]
        for j in range(m + 1):
            cost = row[j]
            if cost == infinity:
                continue
            if i < n and j < m:
                char, probability = table[i][kinds[j]]
                value = cost - math.log(max(probability, 1e-9))
                if value < dp[i + 1][j + 1]:
                    dp[i + 1][j + 1] = value
                    back[i + 1][j + 1] = (i, j, "m")
            if i < n:
                value = cost + SKIP_GLYPH_COST
                if value < dp[i + 1][j]:
                    dp[i + 1][j] = value
                    back[i + 1][j] = (i, j, "g")
            if j < m:
                value = cost + SKIP_CHAR_COST
                if value < dp[i][j + 1]:
                    dp[i][j + 1] = value
                    back[i][j + 1] = (i, j, "c")
    total = dp[n][m]
    if total == infinity:
        return math.inf, "", [], []
    chars: list[str] = []
    probs: list[float] = []
    alignment: list[int] = []
    i, j = n, m
    while i > 0 or j > 0:
        step = back[i][j]
        if step is None:
            break
        pi, pj, kind = step
        if kind == "m":
            char, probability = table[pi][kinds[pj]]
            chars.append(char)
            probs.append(float(probability))
            alignment.append(pi)
        i, j = pi, pj
    chars.reverse()
    probs.reverse()
    alignment.reverse()
    return total / max(n, m), "".join(chars), probs, alignment


def decode(
    glyph_probabilities: list[dict[str, float]],
    *,
    country: str | None = None,
    min_glyphs: int = 4,
) -> Decoded | None:
    """Best plate string across every applicable jurisdiction format."""
    if len(glyph_probabilities) < min_glyphs:
        return None
    table = glyph_probability_table(glyph_probabilities)
    best: Decoded | None = None
    best_cost = math.inf
    for fmt in formats_for(country):
        for total in range(fmt.min_len, fmt.max_len + 1):
            if total > len(glyph_probabilities) + 2:
                continue
            for markers in _expand_counts(fmt.tokens, 0, total, []):
                if not markers:
                    continue
                kinds = [_kind_of(marker) for marker in markers]
                cost, text, probs, alignment = _align(table, kinds)
                if cost == math.inf or not text:
                    continue
                adjusted = cost - 0.11 * math.log(max(fmt.weight, 0.05))
                if adjusted < best_cost:
                    best_cost = adjusted
                    best = Decoded(
                        text=text,
                        score=0.0,
                        format=fmt,
                        probabilities=probs,
                        alignment=alignment,
                        valid=validate_markers(text, markers),
                    )
    if best is None:
        return None
    best.score = round(float(-best_cost), 4)
    return best


def alternates(
    glyph_probabilities: list[dict[str, float]],
    best: Decoded,
    *,
    limit: int = 3,
) -> list[dict[str, object]]:
    """Surface runner-up reads by flipping the weakest-aligned characters."""
    alternates: list[dict[str, object]] = []
    if not best.probabilities or not best.alignment:
        return alternates
    order = sorted(range(len(best.probabilities)), key=lambda k: best.probabilities[k])[:4]
    base_confidence = best.mean_probability
    chars = list(best.text)
    for idx in order:
        glyph_index = best.alignment[idx] if idx < len(best.alignment) else -1
        if glyph_index < 0 or glyph_index >= len(glyph_probabilities):
            continue
        current = chars[idx]
        ranked = sorted(
            glyph_probabilities[glyph_index].items(), key=lambda kv: kv[1], reverse=True
        )
        for char, probability in ranked[:6]:
            if char == current or char not in ALPHABET:
                continue
            variant = chars.copy()
            variant[idx] = char
            candidate = "".join(variant)
            if any(a["plate"] == candidate for a in alternates):
                continue
            alternates.append(
                {
                    "plate": candidate,
                    "confidence": round(float(base_confidence * max(0.12, probability)), 4),
                    "changed_at": idx,
                    "format": best.format.name,
                }
            )
            break
        if len(alternates) >= limit:
            break
    return alternates


def _prob(decoded: Decoded, index: int) -> float:
    if index < len(decoded.probabilities):
        return decoded.probabilities[index]
    return 0.0


def validate(text: str, fmt: Format) -> bool:
    """Strict format check (used by the API and tests)."""
    return any(
        validate_markers(text, markers)
        for markers in _expand_counts(fmt.tokens, 0, len(text), [])
    )


def validate_markers(text: str, markers: list[str]) -> bool:
    if len(text) != len(markers):
        return False
    for char, marker in zip(text, markers):
        if marker == "A" and not char.isalpha():
            return False
        if marker == "D" and not char.isdigit():
            return False
        if not char.isalnum():
            return False
    return True


# ------------------------------------------------------------------ metadata
IN_REGION_CODES = {
    "AP": "Andhra Pradesh", "AR": "Arunachal Pradesh", "AS": "Assam", "BR": "Bihar",
    "CG": "Chhattisgarh", "CH": "Chandigarh", "DD": "Daman & Diu", "DL": "Delhi",
    "DN": "Dadra & Nagar Haveli", "GA": "Goa", "GJ": "Gujarat", "HR": "Haryana",
    "HP": "Himachal Pradesh", "JH": "Jharkhand", "JK": "Jammu & Kashmir",
    "KA": "Karnataka", "KL": "Kerala", "LA": "Ladakh", "LD": "Lakshadweep",
    "MH": "Maharashtra", "ML": "Meghalaya", "MN": "Manipur", "MP": "Madhya Pradesh",
    "MZ": "Mizoram", "NL": "Nagaland", "OD": "Odisha", "OR": "Odisha", "PB": "Punjab",
    "PY": "Puducherry", "RJ": "Rajasthan", "SK": "Sikkim", "TN": "Tamil Nadu",
    "TS": "Telangana", "TR": "Tripura", "UK": "Uttarakhand", "UP": "Uttar Pradesh",
    "WB": "West Bengal",
}

IN_STATE_DISTRICTS = {
    "MH": {"12": "Pune", "01": "Mumbai Central", "02": "Mumbai West", "04": "Thane"},
    "OD": {"02": "Bhubaneswar", "05": "Cuttack", "14": "Berhampur"},
    "KA": {"01": "Bengaluru Central", "05": "Bengaluru South"},
    "DL": {"01": "Delhi North", "08": "New Delhi"},
    "TN": {"01": "Chennai Central", "38": "Coimbatore"},
}

COMMERCIAL_SUFFIXES = ("P", "C", "T", "V", "Y", "G", "B", "J", "K", "Z")


def infer_region(text: str, country: str) -> str | None:
    if country == "IN" and len(text) >= 4:
        state = text[:2]
        if state in IN_REGION_CODES:
            return state
    if country == "US" and len(text) >= 2:
        return "US"
    if country == "UK":
        return "UK"
    if country == "EU":
        return "EU"
    return None


def describe_region(code: str | None, plate: str | None = None) -> str:
    if not code:
        return "Unassigned"
    if code in IN_REGION_CODES:
        name = IN_REGION_CODES[code]
        if plate and len(plate) >= 4:
            district = IN_STATE_DISTRICTS.get(code, {}).get(plate[2:4])
            if district:
                return f"{district}, {name}"
        return name
    return code


def infer_plate_type(text: str, fmt: Format | None) -> str:
    if fmt is None:
        return "private"
    if fmt.plate_type != "private":
        return fmt.plate_type
    if fmt.country == "IN" and len(text) >= 3 and text[:2].isdigit() and "BH" in text[:4]:
        return "commercial"
    return "private"


def risk_score(
    *,
    watchlist_hit: bool,
    watchlist_severity: str = "low",
    confidence: float = 0.0,
    status: str = "pending",
    hour: int = 12,
    duplicate: bool = False,
) -> tuple[float, str]:
    """Deterministic 0-100 risk score used for triage ordering."""
    score = 6.0
    if watchlist_hit:
        score += {"low": 40, "medium": 55, "high": 70, "critical": 88}.get(watchlist_severity, 45)
    if confidence < 0.55:
        score += 12
    if status == "flagged":
        score += 18
    if status == "denied":
        score += 26
    if duplicate:
        score += 4
    if hour >= 22 or hour <= 5:
        score += 8
    score = max(0.0, min(100.0, score))
    level = "critical" if score >= 80 else "high" if score >= 58 else "medium" if score >= 34 else "low"
    return round(score, 1), level
