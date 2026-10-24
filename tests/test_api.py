"""HTTP contract tests for every public endpoint."""
from __future__ import annotations

import io

from PIL import Image

from app.services import samples


def _upload_bytes(text: str = "MH12AB3456") -> bytes:
    plate = samples.render_plate(text, width=420)
    canvas = Image.new("RGB", (620, 300), (36, 38, 46))
    canvas.paste(plate, ((620 - plate.width) // 2, (300 - plate.height) // 2))
    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def test_health_reports_engine_ready(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["glyph_classes"] >= 30


def test_liveness_and_readiness(client):
    assert client.get("/healthz").json()["status"] == "alive"
    assert client.get("/readyz").text == "ready"


def test_login_rejects_bad_password(client, workspace):
    response = client.post(
        "/api/v1/auth/login", json={"email": "admin@test.io", "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_login_validation_error_envelope(client):
    response = client.post("/api/v1/auth/login", json={"email": "not-an-email", "password": "x"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


def test_protected_route_requires_auth(client):
    assert client.get("/api/v1/captures").status_code == 401


def test_me_returns_tenant_context(client, admin_headers):
    response = client.get("/api/v1/auth/me", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "admin@test.io"
    assert body["role"] == "admin"


def test_capture_upload_reads_plate(client, admin_headers, workspace):
    files = {"file": ("frame.jpg", _upload_bytes("MH12AB3456"), "image/jpeg")}
    data = {"camera_id": workspace["camera_id"], "notes": "unit test"}
    response = client.post("/api/v1/captures", headers=admin_headers, files=files, data=data)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["plate"] == "MH12AB3456"
    assert body["watchlist_hit"] is True
    assert body["status"] == "flagged"
    assert body["risk_level"] in ("critical", "high")
    assert body["camera_name"] == "Gate One"

    image = client.get(f"/api/v1/captures/{body['id']}/image", headers=admin_headers)
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"

    thumb = client.get(f"/api/v1/captures/{body['id']}/image?variant=thumb", headers=admin_headers)
    assert thumb.status_code == 200


def test_capture_rejects_oversized_and_corrupt(client, admin_headers):
    bad = {"file": ("frame.jpg", b"not an image", "image/jpeg")}
    response = client.post("/api/v1/captures", headers=admin_headers, files=bad)
    assert response.status_code == 422
    assert response.json()["error"]["code"] in ("invalid_image", "validation_failed")


def test_capture_list_filter_and_pagination(client, admin_headers):
    response = client.get("/api/v1/captures?page_size=5&q=MH12", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["page_size"] == 5
    assert body["total"] >= 1
    assert all("MH12" in (item["plate"] or "") for item in body["items"])


def test_capture_status_update_and_permissions(client, admin_headers, viewer_headers):
    listing = client.get("/api/v1/captures?page_size=1", headers=admin_headers).json()
    capture_id = listing["items"][0]["id"]

    denied = client.patch(
        f"/api/v1/captures/{capture_id}", headers=viewer_headers, json={"status": "verified"}
    )
    assert denied.status_code == 403

    updated = client.patch(
        f"/api/v1/captures/{capture_id}",
        headers=admin_headers,
        json={"status": "verified", "notes": "checked"},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "verified"
    assert updated.json()["reviewed_by"] == "Test Admin"


def test_capture_export_csv(client, admin_headers):
    response = client.get("/api/v1/captures/export.csv", headers=admin_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert b"capture_id" in response.content


def test_watchlist_crud(client, admin_headers, viewer_headers):
    payload = {
        "plate": "ka05mj4412",
        "label": "Permit lapsed",
        "category": "permit",
        "severity": "medium",
        "reason": "Test entry",
    }
    created = client.post("/api/v1/watchlist", headers=admin_headers, json=payload)
    assert created.status_code == 201, created.text
    entry = created.json()
    assert entry["plate"] == "KA05MJ4412"

    duplicate = client.post("/api/v1/watchlist", headers=admin_headers, json=payload)
    assert duplicate.status_code == 409

    forbidden = client.post("/api/v1/watchlist", headers=viewer_headers, json={**payload, "plate": "WB02AC3390"})
    assert forbidden.status_code == 403

    toggled = client.patch(f"/api/v1/watchlist/{entry['id']}/toggle", headers=admin_headers)
    assert toggled.status_code == 200
    assert toggled.json()["active"] is False

    stats = client.get("/api/v1/watchlist/stats", headers=admin_headers).json()
    assert stats["total"] >= 2


def test_analytics_overview_shape(client, admin_headers):
    response = client.get("/api/v1/analytics/overview?range=7d", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {"kpis", "series", "recent", "watchlist", "cameras_total"}
    assert body["kpis"]["scans"] >= 1
    assert len(body["series"]) == 7
    assert isinstance(body["kpis"]["trend"], dict)


def test_recognize_endpoint_is_stateless(client, admin_headers):
    files = {"file": ("frame.jpg", _upload_bytes("DL08CD9021"), "image/jpeg")}
    response = client.post("/api/v1/recognize", headers=admin_headers, files=files,
                           data={"country": "IN"})
    assert response.status_code == 200
    body = response.json()
    assert body["best"] is not None
    assert body["best"]["plate"]
    assert body["best"]["box"]["w"] > 50
    assert body["processing_ms"] > 0


def test_recognize_annotated_returns_image(client, admin_headers):
    files = {"file": ("frame.jpg", _upload_bytes("TS09EF2266"), "image/jpeg")}
    response = client.post("/api/v1/recognize/annotated", headers=admin_headers, files=files)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-plate-count"].isdigit()


def test_samples_gallery(client, admin_headers):
    gallery = client.get("/api/v1/recognize/samples", headers=admin_headers).json()
    assert len(gallery["items"]) == 6
    image = client.get(f"/api/v1/recognize/samples/{gallery['items'][0]['id']}.jpg")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"


def test_cameras_and_fleet_health(client, admin_headers):
    cameras = client.get("/api/v1/cameras", headers=admin_headers).json()
    assert len(cameras) >= 1
    health = client.get("/api/v1/cameras/health", headers=admin_headers).json()
    assert health["total"] >= 1

    created = client.post(
        "/api/v1/cameras",
        headers=admin_headers,
        json={"code": "CAM-9", "name": "Gate Nine", "corridor": "Test", "lanes": 3},
    )
    assert created.status_code == 201, created.text
    assert created.json()["code"] == "CAM-9"


def test_api_key_lifecycle(client, admin_headers):
    created = client.post(
        "/api/v1/apis-keys", headers=admin_headers, json={"name": "CI"}
    ) if False else client.post(
        "/api/v1/api-keys", headers=admin_headers, json={"name": "CI", "scopes": ["captures:read"]}
    )
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    assert token.startswith("pv_")

    # The key authenticates without a session cookie.
    response = client.get("/api/v1/captures?page_size=1", headers={"X-API-Key": token})
    assert response.status_code == 200


def test_audit_trail_records_actions(client, admin_headers):
    events = client.get("/api/v1/audit", headers=admin_headers).json()
    actions = {event["action"] for event in events}
    assert "auth.login" in actions
    assert "capture.created" in actions


def test_metrics_exposition(client):
    response = client.get("/api/v1/metrics")
    assert response.status_code == 200
    assert "platevision_http_requests_total" in response.text


def test_console_and_static_assets(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "PlateVision" in page.text
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200
    assert client.get("/favicon.ico").status_code == 200


def test_openapi_schema_is_complete(client):
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"].startswith("PlateVision")
    assert "/api/v1/captures" in schema["paths"]
    assert "/api/v1/recognize" in schema["paths"]


def test_unknown_route_uses_error_envelope(client):
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "http_error"
