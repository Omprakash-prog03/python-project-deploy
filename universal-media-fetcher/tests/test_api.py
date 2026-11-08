"""API contract tests.

The behaviour that matters here is not the happy path but the shape of a
refusal: status code, error code, message and alternative must all survive the
trip to the client, because the frontend renders them verbatim rather than
inventing its own copy.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from umf.main import app

client = TestClient(app)


def test_healthz() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["adapters"] >= 22


def test_info_states_what_is_not_built() -> None:
    """§16: no fake success. The API says what it cannot do."""
    body = client.get("/api/v1/info").json()
    assert body["makes_outbound_requests"] is False
    assert any("downloads" in item for item in body["not_implemented"])


def test_platforms_lists_every_integration() -> None:
    body = client.get("/api/v1/platforms").json()
    slugs = {platform["slug"] for platform in body["platforms"]}
    assert {"youtube", "netflix", "internet_archive", "direct_media"} <= slugs
    assert body["count"] == len(body["platforms"])
    assert body["planned_endpoints"], "unbuilt endpoints must be declared"


def test_platforms_reports_capabilities_without_network() -> None:
    platforms = {p["slug"]: p for p in client.get("/api/v1/platforms").json()["platforms"]}
    assert platforms["youtube"]["capabilities"]["authorized_download"] is False
    assert platforms["internet_archive"]["capabilities"]["authorized_download"] is True
    assert platforms["netflix"]["capabilities"]["drm_protected"] is True


# ------------------------------------------------------------------- analyze
def test_analyze_youtube() -> None:
    response = client.post("/api/v1/analyze", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert response.status_code == 200
    body = response.json()
    assert body["platform"] == "youtube"
    assert body["content_id"] == "dQw4w9WgXcQ"
    assert body["capabilities"]["authorized_download"] is False
    assert body["metadata"] is None
    assert body["metadata_status"] == "pending"


def test_analyze_archive_item() -> None:
    body = client.post(
        "/api/v1/analyze", json={"url": "https://archive.org/details/night_of_the_living_dead"}
    ).json()
    assert body["platform"] == "internet_archive"
    assert body["capabilities"]["authorized_download"] is True


def test_analyze_netflix_explains_drm() -> None:
    response = client.post("/api/v1/analyze", json={"url": "https://www.netflix.com/title/80100172"})
    assert response.status_code == 200
    body = response.json()
    assert body["capabilities"]["drm_protected"] is True
    assert body["metadata_status"] == "unavailable"
    assert "DRM_PROTECTED" in body["metadata_note"]
    assert "Netflix" in body["platform_name"]


def test_analyze_direct_media() -> None:
    body = client.post("/api/v1/analyze", json={"url": "https://cdn.example.com/a/clip.mp4"}).json()
    assert body["platform"] == "direct_media"
    assert body["media_kind"] == "video"
    assert body["capabilities"]["authorized_download"] is True


# ------------------------------------------------------------------ refusals
def test_ssrf_target_is_refused_with_400() -> None:
    response = client.post(
        "/api/v1/analyze", json={"url": "http://169.254.169.254/latest/meta-data/"}
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "INVALID_URL"


def test_unsupported_platform_is_422_not_400() -> None:
    response = client.post("/api/v1/analyze", json={"url": "https://example.com/page"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_PLATFORM"


def test_empty_url_is_rejected_by_validation() -> None:
    assert client.post("/api/v1/analyze", json={"url": ""}).status_code == 422


def test_missing_url_is_rejected() -> None:
    assert client.post("/api/v1/analyze", json={}).status_code == 422


# --------------------------------------------------------------------- batch
def test_batch_isolates_failures() -> None:
    response = client.post(
        "/api/v1/analyze/batch",
        json={
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "urls": ["http://127.0.0.1/", "https://example.com/x", "https://archive.org/details/x"],
        },
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False and results[1]["error"]["code"] == "INVALID_URL"
    assert results[2]["ok"] is False and results[2]["error"]["code"] == "UNSUPPORTED_PLATFORM"
    assert results[3]["ok"] is True


def test_batch_limit_is_enforced() -> None:
    response = client.post(
        "/api/v1/analyze", json={"url": "https://youtu.be/dQw4w9WgXcQ", "urls": ["https://a.b/"] * 21}
    )
    assert response.status_code == 422
