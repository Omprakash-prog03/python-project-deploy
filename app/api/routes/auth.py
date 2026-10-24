"""Authentication: cookie sessions + bearer tokens."""
from __future__ import annotations

from datetime import UTC, datetime

import jwt
from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from app.api.deps import (
    SESSION_COOKIE,
    CurrentUser,
    DbSession,
    _token_from_request,
    authenticate,
    client_ip,
)
from app.core.config import settings
from app.core.errors import Unauthorized
from app.core.security import create_token, decode_token
from app.models.entities import Organization, User
from app.schemas.models import LoginRequest, TokenResponse, UserOut
from app.services import audit
from app.services.serializers import capture_out  # noqa: F401  (re-export convenience)

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_payload(db, user: User) -> dict:
    org = db.get(Organization, user.org_id)
    from sqlalchemy import func

    from app.models.entities import Capture

    used = db.scalar(
        select(func.count(Capture.id)).where(Capture.org_id == user.org_id)
    ) or 0
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "avatar_initials": user.avatar_initials,
        "zone": user.zone,
        "org_id": user.org_id,
        "organization": org.name if org else None,
        "plan": org.plan if org else None,
        "quota_used": int(used),
        "quota_total": org.monthly_scan_quota if org else None,
    }


def _issue(response: Response, db, user: User) -> TokenResponse:
    access = create_token(user.id, org_id=user.org_id, role=user.role, kind="access")
    refresh = create_token(user.id, org_id=user.org_id, role=user.role, kind="refresh")
    response.set_cookie(
        SESSION_COOKIE,
        access,
        max_age=settings.access_token_ttl_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        domain=settings.cookie_domain,
        path="/",
    )
    response.set_cookie(
        "pv_refresh",
        refresh,
        max_age=settings.refresh_token_ttl_days * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        domain=settings.cookie_domain,
        path="/api/v1/auth",
    )
    user.last_login_at = datetime.now(UTC)
    db.add(user)
    db.commit()
    return TokenResponse(
        access_token=access,
        expires_in=settings.access_token_ttl_minutes * 60,
        user=UserOut(**_user_payload(db, user)),
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, response: Response, request: Request, db: DbSession) -> TokenResponse:
    user = authenticate(db, payload.email, payload.password)
    if user is None:
        audit.record(
            db,
            org_id="system",
            actor=payload.email,
            action="auth.failed",
            ip=client_ip(request),
        )
        raise Unauthorized("Incorrect email or password.", code="invalid_credentials")
    result = _issue(response, db, user)
    audit.record(
        db,
        org_id=user.org_id,
        actor=user.email,
        action="auth.login",
        entity_type="user",
        entity_id=user.id,
        ip=client_ip(request),
        commit=True,
    )
    return result


@router.post("/demo", response_model=TokenResponse)
def demo_login(response: Response, request: Request, db: DbSession) -> TokenResponse:
    if not settings.allow_demo_login:
        raise Unauthorized("Demo access is disabled for this deployment.", code="demo_disabled")
    user = authenticate(db, settings.demo_email, settings.demo_password)
    if user is None:
        raise Unauthorized("Demo workspace is not provisioned.", code="demo_unavailable")
    result = _issue(response, db, user)
    audit.record(
        db,
        org_id=user.org_id,
        actor=user.email,
        action="auth.demo_login",
        ip=client_ip(request),
    )
    return result


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: Request, response: Response, db: DbSession) -> TokenResponse:
    token = request.cookies.get("pv_refresh") or _token_from_request(
        request, request.headers.get("authorization")
    )
    if not token:
        raise Unauthorized("Missing refresh token.", code="missing_credentials")
    try:
        payload = decode_token(token, expected_kind="refresh")
    except jwt.PyJWTError as exc:
        raise Unauthorized("Refresh token is invalid or expired.", code="invalid_token") from exc
    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise Unauthorized("Account is inactive.", code="inactive_user")
    return _issue(response, db, user)


@router.post("/logout")
def logout(response: Response, user: CurrentUser) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie("pv_refresh", path="/api/v1/auth")
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser, db: DbSession) -> UserOut:
    return UserOut(**_user_payload(db, user))
