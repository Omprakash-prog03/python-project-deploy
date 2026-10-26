"""Rotating 6-digit access code for Supreme Control.

One code, shared by every administrator, that changes every ten minutes. The
code is derived from a secret plus the clock (RFC 6238 TOTP with a 600-second
period), so the page that displays it and the login that checks it compute the
same value independently. Nothing is stored per request and nothing is emailed —
a purely random code would have to be persisted, would break across multiple
worker processes, and would leak if the database were read.

Standard library only: drop this file into a Flask, Django or FastAPI project
without adding a dependency.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import struct
import time
from dataclasses import dataclass

PERIOD_SECONDS = 600          # ten minutes
DIGITS = 6
DRIFT_WINDOWS = 1             # tolerate one window either side for clock skew


@dataclass(frozen=True)
class Window:
    """A code and the lifetime of the window it belongs to."""

    counter: int
    code: str
    expires_at: float
    seconds_left: int

    @property
    def formatted(self) -> str:
        """``"123456"`` -> ``"123 456"``, which is easier to read aloud."""
        half = DIGITS // 2
        return f"{self.code[:half]} {self.code[half:]}"


def counter_at(now: float) -> int:
    """Which ten-minute window a timestamp falls in."""
    return int(now // PERIOD_SECONDS)


def hotp(secret: bytes, counter: int) -> str:
    """Derive the fixed-width code for a window from the secret."""
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha256).digest()
    offset = digest[-1] & 0x0F                                   # dynamic truncation
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % 10**DIGITS).zfill(DIGITS)


def code_at(secret: bytes, now: float | None = None) -> Window:
    """The code on screen right now, plus how long it has left."""
    moment = time.time() if now is None else now
    counter = counter_at(moment)
    expires_at = (counter + 1) * PERIOD_SECONDS
    return Window(
        counter=counter,
        code=hotp(secret, counter),
        expires_at=expires_at,
        seconds_left=max(1, int(round(expires_at - moment))),
    )


def normalise(submitted: str | None) -> str:
    """Accept what a human types: ``"123 456"``, ``"123456"``, ``" 123-456 "``."""
    cleaned = (submitted or "").strip().replace(" ", "").replace("-", "")
    return cleaned


def verify(secret: bytes, submitted: str | None, now: float | None = None) -> int | None:
    """Return the matching window counter, or ``None`` if the code is wrong.

    Comparison is constant-time so a wrong code cannot be narrowed down by
    timing. The previous and next windows are accepted because the code crosses
    a minute boundary mid-use and because two servers may disagree about the
    clock by a second or two.
    """
    code = normalise(submitted)
    if not code.isdigit() or len(code) != DIGITS:
        return None
    moment = time.time() if now is None else now
    counter = counter_at(moment)
    for offset in range(-DRIFT_WINDOWS, DRIFT_WINDOWS + 1):
        candidate = counter + offset
        if hmac.compare_digest(hotp(secret, candidate), code):
            return candidate
    return None


def new_secret() -> bytes:
    """256 bits, generated once and stored server-side."""
    return secrets.token_bytes(32)
