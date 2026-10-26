"""Walk the whole sign-in flow, the way the Supreme Control page drives it.

    python demo.py

Uses a temporary database, so it changes nothing. The ``now`` parameter is
threaded through every call so "ten minutes later" needs no waiting.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from auth import login
from otp import PERIOD_SECONDS, code_at
from store import add_admin, connect, ensure_secret, rotate_secret

BAR = "-" * 62


def show(outcome: str, result) -> None:
    mark = "GRANTED" if result.ok else "DENIED "
    detail = f"  ({result.reason})" if result.reason else ""
    print(f"  [{mark}] {outcome}{detail}")


def main() -> None:
    directory = tempfile.mkdtemp(prefix="supreme-demo-")
    conn = connect(str(Path(directory) / "supreme.db"))

    secret = ensure_secret(conn)
    add_admin(conn, "om-2028", "correct-horse", role="Owner")
    add_admin(conn, "anurag", "another-password")

    # A fixed "now" inside a window, then we jump forward by window.
    now = 1_699_999_800.0
    window = code_at(secret, now)

    print(BAR)
    print("SUPREME CONTROL — what the page displays")
    print(BAR)
    print(f"  access code : {window.formatted}")
    print(f"  valid for   : {window.seconds_left} s  (rotates every 10 minutes)")
    print()

    print(BAR)
    print("ADMINISTRATOR SIGN-IN")
    print(BAR)
    show("om-2028, correct password, no code",
         login(conn, secret, "om-2028", "correct-horse", "", now=now))
    show("om-2028, correct password, wrong code",
         login(conn, secret, "om-2028", "correct-horse", "000000", now=now))
    show("om-2028, wrong password, right code",
         login(conn, secret, "om-2028", "nope", window.code, now=now))
    show("anurag, correct password, right code",
         login(conn, secret, "anurag", "another-password", window.code, now=now))
    show("om-2028, correct password, right code",
         login(conn, secret, "om-2028", "correct-horse", window.code, now=now))
    print()

    print(BAR)
    print("REPLAY — the same code a second time")
    print(BAR)
    show("om-2028, same code again",
         login(conn, secret, "om-2028", "correct-horse", window.code, now=now + 5))
    print()

    print(BAR)
    print("NEXT WINDOW — ten minutes later")
    print(BAR)
    later = now + PERIOD_SECONDS
    next_window = code_at(secret, later)
    print(f"  access code : {next_window.formatted}  (was {window.formatted})")
    show("om-2028, new code",
         login(conn, secret, "om-2028", "correct-horse", next_window.code, now=later))
    show("om-2028, the earlier code (already spent)",
         login(conn, secret, "om-2028", "correct-horse", window.code, now=later))
    print()

    print(BAR)
    print("SECRET ROTATION — every outstanding code dies")
    print(BAR)
    after = now + 2 * PERIOD_SECONDS
    old_code = code_at(secret, after)          # what the page showed before rotating
    print(f"  code shown before rotation : {old_code.formatted}")
    secret = rotate_secret(conn)                # the page now derives from a new secret
    print(f"  code shown after rotation  : {code_at(secret, after).formatted}")
    show("om-2028, the pre-rotation code",
         login(conn, secret, "om-2028", "correct-horse", old_code.code, now=after))
    show("om-2028, the post-rotation code",
         login(conn, secret, "om-2028", "correct-horse",
               code_at(secret, after).code, now=after))
    print()
    print(BAR)
    print("The gate is enforced: no valid code, no session.")


if __name__ == "__main__":
    main()
