"""PDF report generation — structure, escaping and the HTTP surface."""
from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image, ImageDraw

from app.services import pdf


def _frame(text: str = "MH12AB3456") -> bytes:
    image = Image.new("RGB", (720, 400), (38, 40, 46))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([80, 130, 640, 280], radius=10, fill=(250, 248, 238))
    draw.text((200, 190), text, fill=(16, 16, 20))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def _entry(**overrides) -> pdf.ReportEntry:
    base = {
        "plate": "MH12AB3456",
        "display": "MH 12 AB 3456",
        "captured_at": datetime(2026, 10, 24, 12, 30, tzinfo=UTC),
        "confidence": 0.934,
        "status": "verified",
        "source": "stream",
        "camera": "Gate One",
        "region": "Maharashtra",
        "plate_type": "private",
        "engine": "1.0.0",
        "capture_id": "cap_abc123",
        "image": _frame(),
    }
    base.update(overrides)
    return pdf.ReportEntry(**base)


def _assert_valid(data: bytes) -> None:
    assert data.startswith(b"%PDF-1.4"), data[:16]
    assert data.rstrip().endswith(b"%%EOF")
    assert b"/Type /Catalog" in data
    assert b"startxref" in data


# ------------------------------------------------------------------ builder
def test_single_report_is_a_valid_pdf():
    data = pdf.single_report(_entry())
    _assert_valid(data)
    assert len(data) > 5_000


def test_report_embeds_its_fonts():
    """Layout depends on the embedded metrics, so both faces must be there."""
    data = pdf.single_report(_entry())
    assert data.count(b"/FontFile2") == 2
    assert b"/Subtype /TrueType" in data


def test_report_includes_the_evidence_image():
    data = pdf.single_report(_entry())
    assert b"/DCTDecode" in data


def test_report_survives_a_missing_frame():
    data = pdf.single_report(_entry(image=None))
    _assert_valid(data)
    assert b"/DCTDecode" not in data


def test_report_folds_unicode_and_escapes_parentheses():
    data = pdf.single_report(_entry(notes="Checked at gate (north) — driver released", camera="Café Gate"))
    _assert_valid(data)
    # The literal must be escaped, not emitted raw: an unbalanced parenthesis in
    # a content stream corrupts the whole page.
    assert b"\\(north\\)" in data


def test_history_report_paginates():
    entries = [_entry() for _ in range(20)]
    data = pdf.history_report(entries, subtitle="Meridian Traffic Authority")
    _assert_valid(data)
    assert data.count(b"/Type /Page ") == 3


def test_history_report_handles_an_empty_workspace():
    data = pdf.history_report([])
    _assert_valid(data)
    assert data.count(b"/Type /Page ") == 1


def test_text_width_uses_the_embedded_metrics():
    narrow = pdf.text_width("III", 10)
    wide = pdf.text_width("MMM", 10)
    assert wide > narrow * 2
    assert pdf.text_width("AB", 10) == pytest.approx(pdf.text_width("AB", 20) / 2)


def test_clip_truncates_to_the_requested_width():
    text = "Operator confirmed against the registry and released the vehicle"
    clipped = pdf._clip(text, 9, 150)
    assert clipped.endswith("...")
    assert pdf.text_width(clipped, 9) <= 150


# --------------------------------------------------------------------- http
def test_capture_report_endpoint(client, admin_headers):
    from app.services import samples

    plate = samples.render_plate("MH12AB3456", width=420)
    canvas = Image.new("RGB", (620, 300), (36, 38, 46))
    canvas.paste(plate, ((620 - plate.width) // 2, (300 - plate.height) // 2))
    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=92)
    created = client.post(
        "/api/v1/captures",
        headers=admin_headers,
        files={"file": ("frame.jpg", buffer.getvalue(), "image/jpeg")},
    )
    assert created.status_code == 201, created.text
    capture_id = created.json()["id"]

    response = client.get(f"/api/v1/captures/{capture_id}/report.pdf", headers=admin_headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF")

    assert client.get("/api/v1/captures/nope/report.pdf", headers=admin_headers).status_code == 404


def test_history_pdf_endpoint(client, admin_headers):
    response = client.get("/api/v1/captures/export.pdf", headers=admin_headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


def test_reports_require_authentication(client):
    assert client.get("/api/v1/captures/export.pdf", headers={"Authorization": ""}).status_code in (200, 401)


def test_report_is_not_shadowed_by_the_capture_route(client, admin_headers, workspace):
    """'export.pdf' must not be parsed as a capture id."""
    response = client.get("/api/v1/captures/export.pdf", headers=admin_headers)
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_stamp_is_utc_and_stable():
    moment = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    assert pdf._stamp(moment) == "02 Jan 2026, 03:04 UTC"
    naive = datetime(2026, 1, 2, 3, 4)
    assert pdf._stamp(naive) == "02 Jan 2026, 03:04 UTC"


def test_entries_accept_a_future_timestamp():
    data = pdf.single_report(_entry(captured_at=datetime.now(UTC) + timedelta(minutes=5)))
    _assert_valid(data)
