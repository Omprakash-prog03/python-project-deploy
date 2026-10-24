"""Password hashing (scrypt) and JWT helpers.

Uses only the standard library + PyJWT so the deploy footprint stays small and
there are no native build dependencies.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt

from app.core.config import settings

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LEN = 32


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return ``scrypt$<salt_hex>$<hash_hex>``."""
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_LEN,
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = encoded.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(
        password.encode("utf-8"),
        salt=bytes.fromhex(salt_hex),
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_LEN,
    )
    return hmac.compare_digest(candidate.hex(), digest_hex)


def create_token(
    subject: str,
    *,
    org_id: str,
    role: str,
    kind: Literal["access", "refresh"] = "access",
    extra: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    ttl = (
        timedelta(minutes=settings.access_token_ttl_minutes)
        if kind == "access"
        else timedelta(days=settings.refresh_token_ttl_days)
    )
    payload: dict[str, Any] = {
        "sub": subject,
        "org": org_id,
        "role": role,
        "typ": kind,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": secrets.token_hex(8),
        "iss": settings.app_name,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_token(token: str, *, expected_kind: str = "access") -> dict[str, Any]:
    payload = jwt.decode(
        token,
        settings.secret_key,
        algorithms=["HS256"],
        issuer=settings.app_name,
        options={"require": ["exp", "sub", "typ"]},
    )
    if payload.get("typ") != expected_kind:
        raise jwt.InvalidTokenError("unexpected token type")
    return payload


def sign_payload(secret: str, body: bytes) -> str:
    """HMAC-SHA256 signature used for outbound webhooks."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
