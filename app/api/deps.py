"""FastAPI dependencies: authentication, tenancy and rate limiting."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import Forbidden, RateLimited, Unauthorized
from app.core.security import decode_token, verify_password
from app.db.session import get_db
from app.models.entities import ApiKey, Organization, User

SESSION_COOKIE = "pv_session"
ROLE_ORDER = {"viewer": 1, "operator": 2, "supervisor": 3, "admin": 4}

DbSession = Annotated[Session, Depends(get_db)]


def _token_from_request(request: Request, authorization: str | None) -> str | None:
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token.strip()
    cookie = request.cookies.get(SESSION_COOKIE)
    return cookie or None


def get_current_user(
    request: Request,
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> User:
    token = _token_from_request(request, authorization)
    if x_api_key:
        return _user_from_api_key(db, x_api_key)
    if not token:
        raise Unauthorized("Authentication required.", code="missing_credentials")
    try:
        payload = decode_token(token, expected_kind="access")
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("Session expired. Please sign in again.", code="token_expired") from exc
    except jwt.PyJWTError as exc:
        raise Unauthorized("Invalid session token.", code="invalid_token") from exc
    user = db.get(User, payload.get("sub", ""))
    if user is None or not user.is_active:
        raise Unauthorized("Account is inactive.", code="inactive_user")
    return user


def _user_from_api_key(db: Session, raw_key: str) -> User:
    from app.core.security import verify_password as _verify  # local import to avoid cycles

    prefix = raw_key[:10]
    candidates = db.scalars(
        select(ApiKey).where(ApiKey.prefix == prefix, ApiKey.active.is_(True))
    ).all()
    for key in candidates:
        if _verify(raw_key, key.key_hash):
            key.last_used_at = datetime.now(UTC)
            db.commit()
            user = db.scalars(
                select(User).where(User.org_id == key.org_id, User.role == "admin").limit(1)
            ).first()
            if user is None:
                raise Unauthorized("API key owner has no active user.", code="invalid_api_key")
            return user
    raise Unauthorized("Invalid API key.", code="invalid_api_key")


def require_role(minimum: str) -> Callable[[User], User]:
    def _guard(user: Annotated[User, Depends(get_current_user)]) -> User:
        if ROLE_ORDER.get(user.role, 0) < ROLE_ORDER.get(minimum, 0):
            raise Forbidden(f"Role '{minimum}' or higher is required.", code="insufficient_role")
        return user

    return _guard


CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_role("admin"))]
SupervisorUser = Annotated[User, Depends(require_role("supervisor"))]


def get_org(user: CurrentUser, db: DbSession) -> Organization:
    org = db.get(Organization, user.org_id)
    if org is None:
        raise Unauthorized("Organization not found.", code="orphan_user")
    return org


Org = Annotated[Organization, Depends(get_org)]


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalars(select(User).where(User.email == email.lower().strip())).first()
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def client_ip(request: Request) -> str:
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(request: Request) -> None:
    """Applied per-router; keyed by API key or client IP."""
    from app.core.ratelimit import TokenBucketLimiter

    limiter: TokenBucketLimiter = request.app.state.limiter
    identity = request.headers.get("x-api-key") or client_ip(request)
    route = request.url.path
    cost = 3.0 if route.endswith(("/recognize", "/captures")) and request.method == "POST" else 1.0
    allowed, retry_after, remaining = limiter.allow(f"{identity}:{route}", cost=cost)
    request.state.rate_remaining = remaining
    if not allowed:
        raise RateLimited(
            "Rate limit exceeded. Slow down or contact your account manager.",
            details={"retry_after_seconds": retry_after},
        )
