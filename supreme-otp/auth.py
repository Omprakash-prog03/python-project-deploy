"""The sign-in gate: username + password + the current 6-digit code, or nothing.

This is the piece that makes the code mandatory. Every rejection path returns the
same shape, so a caller cannot tell "wrong password" from "wrong code" by the
exception it gets — the message is generic, the audit row is specific.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from otp import verify
from store import (
    Admin,
    check_password,
    find_admin,
    mark_counter_used,
    recent_failures,
    record_attempt,
)

# Five wrong codes in a minute locks that username out for the rest of it. A
# valid password plus six digits is one in a million per try, but nothing stops
# someone scripting it — this does.
MAX_FAILURES_PER_MINUTE = 5
FAILURE_WINDOW_SECONDS = 60


@dataclass(frozen=True)
class Result:
    ok: bool
    admin: Admin | None = None
    reason: str = ""
    message: str = ""

    @classmethod
    def granted(cls, admin: Admin) -> Result:
        return cls(ok=True, admin=admin)

    @classmethod
    def denied(cls, reason: str, message: str) -> Result:
        return cls(ok=False, reason=reason, message=message)


GENERIC = "Incorrect username, password, or access code."


def login(
    conn: sqlite3.Connection,
    secret: bytes,
    username: str,
    password: str,
    access_code: str,
    *,
    now: float | None = None,
    allow_code_reuse: bool = False,
) -> Result:
    """Authenticate an administrator.

    All three factors are required. Omitting ``access_code`` fails; passing the
    wrong one fails; passing a code that has already been used in this window
    fails unless ``allow_code_reuse`` is set.
    """
    moment = time.time() if now is None else now
    username = (username or "").strip()

    # ---------------------------------------------------------- rate limit
    if recent_failures(conn, username, moment - FAILURE_WINDOW_SECONDS) >= MAX_FAILURES_PER_MINUTE:
        record_attempt(conn, username, "failure", "rate_limited", moment)
        return Result.denied(
            "rate_limited", "Too many attempts. Try again in a minute."
        )

    def fail(reason: str) -> Result:
        record_attempt(conn, username, "failure", reason, moment)
        return Result.denied(reason, GENERIC)

    # ------------------------------------------------------------ identity
    admin = find_admin(conn, username)
    if admin is None:
        # Hash anyway: doing nothing here makes an unknown username visibly
        # faster than a known one, which enumerates accounts.
        check_password(password, "scrypt$" + "00" * 16 + "$" + "00" * 32)
        return fail("unknown_user")
    if not admin.active:
        return fail("inactive_user")
    if not check_password(password or "", admin.password_hash):
        return fail("bad_password")

    # --------------------------------------------------------- access code
    if not (access_code or "").strip():
        return fail("missing_code")

    counter = verify(secret, access_code, moment)
    if counter is None:
        return fail("bad_code")

    if not allow_code_reuse and counter <= admin.last_otp_counter:
        return fail("reused_code")

    # ------------------------------------------------------------ grant
    mark_counter_used(conn, admin.id, counter)
    record_attempt(conn, username, "success", None, moment)
    return Result.granted(admin)
