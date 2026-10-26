"""Tests for the Supreme Control sign-in gate.

Run from this directory:  python -m pytest test_supreme_otp.py -q
"""
from __future__ import annotations

import pytest
from auth import MAX_FAILURES_PER_MINUTE, login
from otp import (
    DIGITS,
    PERIOD_SECONDS,
    code_at,
    counter_at,
    hotp,
    normalise,
    verify,
)
from store import add_admin, connect, ensure_secret, find_admin, rotate_secret

SECRET = b"a" * 32
# Fix a moment at the *start* of a window so the arithmetic is readable and
# repeatable: 1699999800 / 600 = 2833333 exactly, so 600 s remain.
NOW = 1_699_999_800.0
WINDOW = counter_at(NOW)


@pytest.fixture()
def conn(tmp_path):
    connection = connect(str(tmp_path / "supreme.db"))
    yield connection
    connection.close()


# ------------------------------------------------------------------ the code
def test_code_is_six_digits():
    window = code_at(SECRET, NOW)
    assert len(window.code) == DIGITS
    assert window.code.isdigit()


def test_code_is_stable_within_a_window():
    """The page and the login must agree, so the code cannot drift mid-window."""
    assert code_at(SECRET, NOW).code == code_at(SECRET, NOW + 599).code


def test_code_changes_at_the_boundary():
    assert code_at(SECRET, NOW).code != code_at(SECRET, NOW + PERIOD_SECONDS).code


def test_code_rotates_every_ten_minutes():
    assert PERIOD_SECONDS == 600
    seen = {code_at(SECRET, NOW + step * PERIOD_SECONDS).code for step in range(6)}
    assert len(seen) == 6, "consecutive windows must not repeat"


def test_code_differs_per_secret():
    """Two deployments must never share a code sequence."""
    assert code_at(SECRET, NOW).code != code_at(b"b" * 32, NOW).code


def test_countdown_counts_down():
    assert code_at(SECRET, NOW).seconds_left == PERIOD_SECONDS
    assert code_at(SECRET, NOW + 60).seconds_left == PERIOD_SECONDS - 60


def test_formatted_code_has_a_readable_gap():
    assert code_at(SECRET, NOW).formatted.replace(" ", "").isdigit()
    assert len(code_at(SECRET, NOW).formatted) == DIGITS + 1


# --------------------------------------------------------------- verification
def test_accepts_the_current_code():
    code = code_at(SECRET, NOW).code
    assert verify(SECRET, code, NOW) == WINDOW


def test_rejects_a_wrong_code():
    wrong = "000000" if code_at(SECRET, NOW).code != "000000" else "111111"
    assert verify(SECRET, wrong, NOW) is None


def test_rejects_empty_and_malformed():
    for value in ("", None, "   ", "abc123", "12345", "1234567", "12 34 5"):
        assert verify(SECRET, value, NOW) is None


def test_tolerates_human_formatting():
    code = code_at(SECRET, NOW).code
    spaced = f"{code[:3]} {code[3:]}"
    assert verify(SECRET, spaced, NOW) == WINDOW
    assert verify(SECRET, f" {code} ", NOW) == WINDOW
    assert normalise(spaced) == code


def test_accepts_the_previous_window_for_clock_skew():
    previous = code_at(SECRET, NOW - PERIOD_SECONDS).code
    assert verify(SECRET, previous, NOW) == WINDOW - 1


def test_rejects_an_expired_code():
    """Twenty minutes old is two windows back — outside the drift allowance."""
    stale = code_at(SECRET, NOW - 2 * PERIOD_SECONDS).code
    assert verify(SECRET, stale, NOW) is None


def test_rejects_a_code_from_a_different_secret():
    other = code_at(b"z" * 32, NOW).code
    assert verify(SECRET, other, NOW) is None


def test_hotp_is_deterministic():
    assert hotp(SECRET, WINDOW) == hotp(SECRET, WINDOW)


# --------------------------------------------------------------------- login
def test_login_requires_the_code(conn):
    """The whole point: a correct password alone must not get in."""
    add_admin(conn, "om-2028", "correct-horse")
    result = login(conn, SECRET, "om-2028", "correct-horse", "", now=NOW)
    assert result.ok is False
    assert result.reason == "missing_code"
    assert result.admin is None


def test_login_rejects_a_wrong_code(conn):
    add_admin(conn, "om-2028", "correct-horse")
    wrong = "000000" if code_at(SECRET, NOW).code != "000000" else "111111"
    result = login(conn, SECRET, "om-2028", "correct-horse", wrong, now=NOW)
    assert result.ok is False
    assert result.reason == "bad_code"


def test_login_rejects_a_wrong_password(conn):
    add_admin(conn, "om-2028", "correct-horse")
    code = code_at(SECRET, NOW).code
    result = login(conn, SECRET, "om-2028", "wrong", code, now=NOW)
    assert result.ok is False
    assert result.reason == "bad_password"


def test_login_rejects_an_unknown_user(conn):
    code = code_at(SECRET, NOW).code
    result = login(conn, SECRET, "nobody", "whatever", code, now=NOW)
    assert result.ok is False
    assert result.reason == "unknown_user"


def test_login_succeeds_with_all_three_factors(conn):
    add_admin(conn, "om-2028", "correct-horse", role="Owner")
    code = code_at(SECRET, NOW).code
    result = login(conn, SECRET, "om-2028", "correct-horse", code, now=NOW)
    assert result.ok is True
    assert result.admin is not None
    assert result.admin.username == "om-2028"
    assert result.admin.role == "Owner"


def test_a_code_cannot_be_replayed(conn):
    """One use per administrator per window, or a shoulder-surfed code is a key."""
    add_admin(conn, "om-2028", "correct-horse")
    code = code_at(SECRET, NOW).code
    assert login(conn, SECRET, "om-2028", "correct-horse", code, now=NOW).ok is True

    again = login(conn, SECRET, "om-2028", "correct-horse", code, now=NOW + 5)
    assert again.ok is False
    assert again.reason == "reused_code"


def test_reuse_can_be_allowed_explicitly(conn):
    """Two devices, one window. Off by default; the operator opts in."""
    add_admin(conn, "om-2028", "correct-horse")
    code = code_at(SECRET, NOW).code
    first = login(conn, SECRET, "om-2028", "correct-horse", code, now=NOW)
    second = login(
        conn, SECRET, "om-2028", "correct-horse", code, now=NOW + 5, allow_code_reuse=True
    )
    assert first.ok and second.ok


def test_next_window_works_after_a_previous_login(conn):
    """Using a code must not lock the account out of the following window."""
    add_admin(conn, "om-2028", "correct-horse")
    first = code_at(SECRET, NOW).code
    assert login(conn, SECRET, "om-2028", "correct-horse", first, now=NOW).ok
    later = NOW + PERIOD_SECONDS
    second = code_at(SECRET, later).code
    assert login(conn, SECRET, "om-2028", "correct-horse", second, now=later).ok


def test_shared_code_works_for_every_administrator(conn):
    """One code on the supreme page, many admins — as specified."""
    add_admin(conn, "om-2028", "pw-one")
    add_admin(conn, "anurag", "pw-two")
    code = code_at(SECRET, NOW).code
    assert login(conn, SECRET, "om-2028", "pw-one", code, now=NOW).ok
    assert login(conn, SECRET, "anurag", "pw-two", code, now=NOW).ok


def test_inactive_administrator_cannot_log_in(conn):
    admin_id = add_admin(conn, "anurag", "pw-two")
    conn.execute("UPDATE administrators SET active = 0 WHERE id = ?", (admin_id,))
    conn.commit()
    code = code_at(SECRET, NOW).code
    result = login(conn, SECRET, "anurag", "pw-two", code, now=NOW)
    assert result.ok is False
    assert result.reason == "inactive_user"


def test_brute_force_is_rate_limited(conn):
    add_admin(conn, "om-2028", "correct-horse")
    for attempt in range(MAX_FAILURES_PER_MINUTE):
        login(conn, SECRET, "om-2028", "wrong", "000000", now=NOW + attempt)
    blocked = login(conn, SECRET, "om-2028", "correct-horse", "123456", now=NOW + 30)
    assert blocked.ok is False
    assert blocked.reason == "rate_limited"


def test_rate_limit_expires(conn):
    add_admin(conn, "om-2028", "correct-horse")
    for attempt in range(MAX_FAILURES_PER_MINUTE):
        login(conn, SECRET, "om-2028", "wrong", "000000", now=NOW + attempt)
    later = NOW + 120
    code = code_at(SECRET, later).code
    assert login(conn, SECRET, "om-2028", "correct-horse", code, now=later).ok


# ------------------------------------------------------------------- storage
def test_secret_persists_and_rotates(conn):
    first = ensure_secret(conn)
    assert ensure_secret(conn) == first, "the secret must be stable across restarts"

    rotated = rotate_secret(conn)
    assert rotated != first

    old_code = code_at(first, NOW).code
    assert verify(rotated, old_code, NOW) is None, "rotation must kill old codes"


def test_failed_attempts_are_recorded(conn):
    add_admin(conn, "om-2028", "correct-horse")
    login(conn, SECRET, "om-2028", "correct-horse", "", now=NOW)
    rows = conn.execute(
        "SELECT outcome, reason FROM login_attempts WHERE username = 'om-2028'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failure"
    assert rows[0]["reason"] == "missing_code"


def test_used_window_is_persisted(conn):
    add_admin(conn, "om-2028", "correct-horse")
    code = code_at(SECRET, NOW).code
    login(conn, SECRET, "om-2028", "correct-horse", code, now=NOW)
    stored = find_admin(conn, "om-2028")
    assert stored is not None
    assert stored.last_otp_counter == WINDOW
