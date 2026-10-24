"""Unit tests for security, rate limiting, metrics and serialisation."""
from __future__ import annotations

import jwt
import pytest

from app.core.config import settings
from app.core.metrics import Metrics
from app.core.ratelimit import TokenBucketLimiter
from app.core.security import (
    constant_time_eq,
    create_token,
    decode_token,
    hash_password,
    sign_payload,
    verify_password,
)


def test_password_hash_roundtrip():
    encoded = hash_password("correct horse battery staple")
    assert encoded.startswith("scrypt$")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong", encoded)


def test_password_salt_is_random():
    assert hash_password("same") != hash_password("same")


def test_jwt_roundtrip_and_ttl():
    token = create_token("user-1", org_id="org-1", role="admin")
    payload = decode_token(token)
    assert payload["sub"] == "user-1"
    assert payload["org"] == "org-1"
    assert payload["exp"] > payload["iat"]


def test_jwt_rejects_wrong_kind_and_tampering():
    refresh = create_token("user-1", org_id="org-1", role="admin", kind="refresh")
    with pytest.raises(jwt.PyJWTError):
        decode_token(refresh, expected_kind="access")
    with pytest.raises(jwt.PyJWTError):
        decode_token(refresh + "x", expected_kind="refresh")


def test_expired_token_rejected():
    token = create_token("user-1", org_id="org-1", role="admin")
    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(
            token,
            settings.secret_key,
            algorithms=["HS256"],
            issuer=settings.app_name,
            options={"verify_exp": False},
        ) and jwt.decode(
            token,
            settings.secret_key,
            algorithms=["HS256"],
            issuer=settings.app_name,
            options={"verify_exp": True, "verify_iat": False},
            leeway=-10_000_000,
        )


def test_webhook_signature_is_stable():
    body = b'{"event":"capture.created"}'
    assert sign_payload("secret", body) == sign_payload("secret", body)
    assert sign_payload("secret", body) != sign_payload("other", body)
    assert constant_time_eq(sign_payload("secret", body), sign_payload("secret", body))


def test_token_bucket_limits_and_refills():
    limiter = TokenBucketLimiter(per_minute=60, burst=3)
    results = [limiter.allow("client")[0] for _ in range(4)]
    assert results[:3] == [True, True, True]
    assert results[3] is False
    allowed, retry_after, remaining = limiter.allow("other")
    assert allowed and retry_after == 0 and remaining >= 0


def test_metrics_snapshot_and_prometheus():
    metrics = Metrics()
    metrics.inc("http_requests", status="200", path="/api/v1/captures")
    metrics.observe("http_request_duration", 0.042)
    metrics.gauge("health_ok", 1.0)
    snapshot = metrics.snapshot()
    assert snapshot["counters"]["http_requests{path=/api/v1/captures,status=200}"] == 1
    assert snapshot["latency"]["http_request_duration"]["p50_ms"] > 0
    exposition = metrics.prometheus()
    assert "platevision_http_requests_total" in exposition
    assert "platevision_health_ok" in exposition


def test_settings_derived_values():
    assert settings.cors_origin_list
    assert "IN" in settings.country_list
    assert settings.alpr_default_country.isupper()


def test_serializers_format_plate():
    from app.services.serializers import _format_plate

    assert _format_plate("MH12AB3456") == "MH 12 AB 3456"
    assert _format_plate(None) == "UNREADABLE"
    assert _format_plate("ABC123") == "ABC123"


def test_region_description_for_known_state():
    from app.alpr import grammar

    assert grammar.describe_region("OD", "OD02BV7788").endswith("Odisha")
    assert grammar.describe_region(None) == "Unassigned"
