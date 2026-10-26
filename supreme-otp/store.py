"""Persistence for the Supreme Control sign-in gate.

Three tables and the handful of queries the login path needs. Plain ``sqlite3``
so it runs anywhere; the SQL is portable to Postgres with a driver swap.

    supreme_settings  one row: the shared code secret
    administrators    username, password hash, last used code window
    login_attempts    every attempt, for rate limiting and the audit trail
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass

from otp import new_secret

SCHEMA = """
CREATE TABLE IF NOT EXISTS supreme_settings (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    otp_secret   BLOB    NOT NULL,
    created_at   REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS administrators (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT    NOT NULL UNIQUE,
    password_hash    TEXT    NOT NULL,
    role             TEXT    NOT NULL DEFAULT 'Admin',
    last_otp_counter INTEGER NOT NULL DEFAULT -1,
    active           INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT    NOT NULL,
    outcome    TEXT    NOT NULL,
    reason     TEXT,
    at         REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempts_user_time
    ON login_attempts (username, at);
"""


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    """``scrypt$<salt_hex>$<hash_hex>`` — standard library, deliberately slow."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against its stored hash."""
    try:
        scheme, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32
    )
    return secrets.compare_digest(digest.hex(), hash_hex)


# --------------------------------------------------------------------- setup
def connect(path: str = "supreme.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def ensure_secret(conn: sqlite3.Connection) -> bytes:
    """Return the shared secret, creating it on first run."""
    row = conn.execute("SELECT otp_secret FROM supreme_settings WHERE id = 1").fetchone()
    if row is not None:
        return bytes(row["otp_secret"])
    secret = new_secret()
    conn.execute(
        "INSERT INTO supreme_settings (id, otp_secret, created_at) VALUES (1, ?, ?)",
        (secret, time.time()),
    )
    conn.commit()
    return secret


def rotate_secret(conn: sqlite3.Connection) -> bytes:
    """Replace the secret. Every outstanding code stops working immediately."""
    secret = new_secret()
    conn.execute(
        "UPDATE supreme_settings SET otp_secret = ?, created_at = ? WHERE id = 1",
        (secret, time.time()),
    )
    conn.commit()
    return secret


def add_admin(conn: sqlite3.Connection, username: str, password: str, role: str = "Admin") -> int:
    cursor = conn.execute(
        "INSERT INTO administrators (username, password_hash, role) VALUES (?, ?, ?)",
        (username, hash_password(password), role),
    )
    conn.commit()
    return int(cursor.lastrowid)


# --------------------------------------------------------------------- reads
@dataclass(frozen=True)
class Admin:
    id: int
    username: str
    password_hash: str
    role: str
    last_otp_counter: int
    active: bool


def find_admin(conn: sqlite3.Connection, username: str) -> Admin | None:
    row = conn.execute(
        "SELECT id, username, password_hash, role, last_otp_counter, active "
        "FROM administrators WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None:
        return None
    return Admin(
        id=int(row["id"]),
        username=row["username"],
        password_hash=row["password_hash"],
        role=row["role"],
        last_otp_counter=int(row["last_otp_counter"]),
        active=bool(row["active"]),
    )


def mark_counter_used(conn: sqlite3.Connection, admin_id: int, counter: int) -> None:
    conn.execute(
        "UPDATE administrators SET last_otp_counter = ? WHERE id = ?", (counter, admin_id)
    )
    conn.commit()


def record_attempt(
    conn: sqlite3.Connection, username: str, outcome: str, reason: str | None, at: float
) -> None:
    conn.execute(
        "INSERT INTO login_attempts (username, outcome, reason, at) VALUES (?, ?, ?, ?)",
        (username, outcome, reason, at),
    )
    conn.commit()


def recent_failures(conn: sqlite3.Connection, username: str, since: float) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM login_attempts "
        "WHERE username = ? AND outcome = 'failure' AND at >= ?",
        (username, since),
    ).fetchone()
    return int(row["n"])
