"""Tests for the optional ParkPow cloud reader and its fallback.

The behaviour worth protecting is not "does it parse a JSON body" but "can this
ever take the service down". Every test below is written from that angle.
"""
from __future__ import annotations

import httpx
import pytest

from app.alpr import remote
from app.alpr.engine import ALPREngine, FallbackEngine, get_engine
from app.alpr.remote import ParkPowEngine, RemoteEngineError, parse_response
from app.core.config import settings

# --------------------------------------------------------------------- fixtures
FLAT_RESULT = {
    "results": [
        {
            "plate": "KA05MJ4412",
            "score": 0.94,
            "dscore": 0.91,
            "box": {"xmin": 12, "ymin": 40, "xmax": 190, "ymax": 96},
            "region": {"code": "in", "score": 0.88},
            "candidates": [
                {"plate": "KA05MJ4412", "score": 0.94},
                {"plate": "KA05MJ441Z", "score": 0.03},
            ],
        }
    ],
    "processing_time": 118.4,
}

NESTED_RESULT = {
    "results": [
        {
            "plate": {
                "value": "MH12AB3456",
                "score": 0.87,
                "box": {"xmin": 5, "ymin": 6, "xmax": 205, "ymax": 70},
            },
            "score": 0.87,
            "region": {"code": "in-mh", "score": 0.8},
        }
    ]
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _engine(**kwargs) -> ParkPowEngine:
    params = {"api_key": "test-key", "sleep": lambda _s: None}
    params.update(kwargs)
    return ParkPowEngine(**params)


# ------------------------------------------------------------------ happy path
def test_recognize_maps_a_flat_response() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read()
        return httpx.Response(200, json=FLAT_RESULT)

    engine = _engine(client=_client(handler))
    result = engine.recognize(_png(200, 120))

    assert seen["url"].endswith("/v1/plate-reader/")
    assert seen["auth"] == "Token test-key"
    assert b'name="upload"' in seen["body"]

    assert len(result.plates) == 1
    plate = result.plates[0]
    assert plate.plate == "KA05MJ4412"
    assert plate.confidence == pytest.approx(0.91)
    assert plate.box.x == 12 and plate.box.w == 178 and plate.box.h == 56
    assert plate.country == "IN"
    assert plate.format_valid is True
    assert result.image_width == 200 and result.image_height == 120


def test_recognize_maps_a_nested_response() -> None:
    engine = _engine(client=_client(lambda _r: httpx.Response(200, json=NESTED_RESULT)))
    plate = engine.recognize(_png(210, 80)).plates[0]
    assert plate.plate == "MH12AB3456"
    assert plate.country == "IN"
    assert plate.box.w == 200


def test_regions_and_camera_are_sent_as_repeatable_fields() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return httpx.Response(200, json={"results": []})

    engine = _engine(
        client=_client(handler), regions=("in", "us-ca"), camera_id="Gate-1"
    )
    engine.recognize(_png(20, 20))
    body = seen["body"]
    assert b'name="regions"' in body and b"in" in body and b"us-ca" in body
    assert b'name="camera_id"' in body and b"Gate-1" in body


def test_empty_results_is_not_an_error() -> None:
    engine = _engine(client=_client(lambda _r: httpx.Response(200, json={"results": []})))
    result = engine.recognize(_png(20, 20))
    assert result.plates == []
    assert result.best is None


# ------------------------------------------------------------------- fail modes
def test_missing_key_fails_before_any_network_call() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("a network call must not happen without a key")

    engine = _engine(api_key="", client=_client(handler))
    assert engine.available is False
    with pytest.raises(RemoteEngineError, match="no API key"):
        engine.recognize(_png(20, 20))


def test_client_error_is_raised_not_retried() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="invalid token")

    engine = _engine(client=_client(handler))
    with pytest.raises(RemoteEngineError) as caught:
        engine.recognize(_png(20, 20))
    assert caught.value.status == 401
    assert calls["n"] == 1, "a bad key must not be retried"


def test_rate_limit_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=FLAT_RESULT)

    result = _engine(client=_client(handler)).recognize(_png(200, 120))
    assert calls["n"] == 2
    assert result.plates[0].plate == "KA05MJ4412"


def test_server_errors_are_retried_then_give_up() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    with pytest.raises(RemoteEngineError) as caught:
        _engine(client=_client(handler), retries=2).recognize(_png(20, 20))
    assert caught.value.status == 503
    assert calls["n"] == 3, "one attempt plus two retries"


def test_timeout_is_raised() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(RemoteEngineError, match="timeout|transport"):
        _engine(client=_client(handler), retries=0).recognize(_png(20, 20))


def test_non_json_body_is_raised() -> None:
    engine = _engine(client=_client(lambda _r: httpx.Response(200, text="<html>oops")))
    with pytest.raises(RemoteEngineError, match="not JSON"):
        engine.recognize(_png(20, 20))


# -------------------------------------------------------------------- fallback
class _Boom:
    name = "boom"

    def recognize(self, _image, _options=None):
        raise RemoteEngineError("upstream on fire")


def test_fallback_returns_the_local_result_when_remote_dies() -> None:
    local = ALPREngine()
    wrapper = FallbackEngine(_Boom(), local)

    result = wrapper.recognize(_png(200, 120))

    assert result.diagnostics["engine"] == "local"
    assert wrapper.classifier is local.classifier


def test_fallback_keeps_a_remote_answer_when_it_has_plates() -> None:
    class _Ok:
        name = "parkpow"

        def recognize(self, _image, _options=None):
            return parse_response(FLAT_RESULT, elapsed_ms=5.0, width=200, height=120)

    wrapper = FallbackEngine(_Ok(), ALPREngine())
    result = wrapper.recognize(_png(200, 120))
    assert result.diagnostics["engine"] == "parkpow"
    assert result.plates[0].plate == "KA05MJ4412"


# ------------------------------------------------------------------- factory
def test_factory_stays_local_without_a_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "alpr_engine", "parkpow")
    monkeypatch.setattr(settings, "parkpow_api_key", None)
    import app.alpr.engine as engine_module

    monkeypatch.setattr(engine_module, "_engine", None)
    engine = get_engine()
    assert isinstance(engine, ALPREngine), "a missing key must leave the deploy working"
    assert settings.parkpow_configured is False


def test_factory_builds_the_wrapper_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "alpr_engine", "parkpow")
    monkeypatch.setattr(settings, "parkpow_api_key", "k-123")
    import app.alpr.engine as engine_module

    monkeypatch.setattr(engine_module, "_engine", None)
    engine = get_engine()
    assert isinstance(engine, FallbackEngine)
    assert engine.primary.available is True


def test_local_engine_is_the_default(monkeypatch) -> None:
    import app.alpr.engine as engine_module

    monkeypatch.setattr(settings, "alpr_engine", "local")
    monkeypatch.setattr(engine_module, "_engine", None)
    assert isinstance(get_engine(), ALPREngine)


# --------------------------------------------------------------------- helpers
def _png(width: int, height: int) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (30, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_parse_response_rejects_a_non_dict() -> None:
    with pytest.raises(RemoteEngineError):
        parse_response([], elapsed_ms=1.0, width=1, height=1)


def test_parse_response_tolerates_missing_results_key() -> None:
    result = parse_response({"processing_time": 5}, elapsed_ms=1.0, width=1, height=1)
    assert result.plates == []


def test_unknown_region_falls_back_to_generic() -> None:
    payload = {"results": [{"plate": "ZZ99ZZ9999", "score": 0.5, "region": {"code": "zz"}}]}
    assert parse_response(payload, elapsed_ms=1.0, width=1, height=1).plates[0].country == "GENERIC"


def test_remote_module_exposes_default_endpoint() -> None:
    assert remote.DEFAULT_BASE_URL.startswith("https://api.platerecognizer.com")
