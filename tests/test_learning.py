"""Glyph learning from operator corrections."""
from __future__ import annotations

import io
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.alpr import learning
from app.services import samples


def _glyph(label: str, seed: int) -> SimpleNamespace:
    rng = np.random.default_rng(seed)
    return SimpleNamespace(label=label, bitmap=rng.random((48, 32), dtype=np.float32))


def _upload_bytes(text: str = "MH12AB3456") -> bytes:
    plate = samples.render_plate(text, width=420)
    canvas = Image.new("RGB", (620, 300), (36, 38, 46))
    canvas.paste(plate, ((620 - plate.width) // 2, (300 - plate.height) // 2))
    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def clean_store():
    learning.reset()
    yield
    learning.reset()


def test_align_pairs_returns_only_substitutions():
    glyphs = [_glyph(char, index) for index, char in enumerate("MH12AB3456")]
    pairs = learning.align_pairs("MH12AB3456", "MH12AB3457", glyphs)
    assert len(pairs) == 1
    label, bitmap = pairs[0]
    assert label == "7"
    assert bitmap.shape == (48, 32)


def test_align_pairs_ignores_shifted_reads():
    # One glyph was lost, so every crop is offset against the spelling.
    glyphs = [_glyph(char, index) for index, char in enumerate("BO2AC3390")]
    assert learning.align_pairs("BO2AC3390", "WB02AC3390", glyphs) == []


def test_align_pairs_rejects_unrelated_plate():
    glyphs = [_glyph(char, index) for index, char in enumerate("MH12AB3456")]
    assert learning.align_pairs("MH12AB3456", "DL9CAF0001", glyphs) == []


def test_store_round_trip_and_reset():
    bitmap = np.full((48, 32), 0.5, dtype=np.float32)
    assert learning.add([("7", bitmap)])["added"] == 1
    assert learning.add([("7", bitmap)])["total"] == 2

    labels, bitmaps = learning.load()
    assert labels == ["7", "7"]
    assert bitmaps.shape == (2, 48, 32)

    stats = learning.stats()
    assert stats["exemplars"] == 2
    assert stats["per_class"] == {"7": 2}

    learning.reset()
    assert learning.stats()["exemplars"] == 0
    assert learning.load()[0] == []


def test_store_rejects_unknown_classes():
    summary = learning.add([("!", np.ones((48, 32), dtype=np.float32)), ("", np.ones((48, 32)))])
    assert summary["added"] == 0


def test_exemplars_join_the_classifier_corpus():
    from app.alpr import ocr

    classifier = ocr.get_classifier()
    before = len(classifier.bank.corpus)
    learning.add([("7", np.full((48, 32), 0.25, dtype=np.float32))])
    classifier.refresh()
    assert len(classifier._corpus) == before + 1
    assert classifier._class_index["7"].size >= 1


def test_teach_endpoint_corrects_and_trains(client, admin_headers, viewer_headers):
    files = {"file": ("frame.jpg", _upload_bytes("MH12AB3456"), "image/jpeg")}
    created = client.post("/api/v1/captures", headers=admin_headers, files=files)
    assert created.status_code == 201, created.text
    capture_id = created.json()["id"]

    viewer = client.post(
        f"/api/v1/captures/{capture_id}/teach", headers=viewer_headers, json={"plate": "MH12AB3457"}
    )
    assert viewer.status_code == 403

    response = client.post(
        f"/api/v1/captures/{capture_id}/teach", headers=admin_headers, json={"plate": "MH12AB3457"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["capture"]["plate"] == "MH12AB3457"
    assert body["learned"]["added"] >= 1
    assert body["stats"]["exemplars"] >= 1

    assert client.get(f"/api/v1/captures/{capture_id}", headers=admin_headers).json()["plate"] == (
        "MH12AB3457"
    )


def test_teach_rejects_unknown_capture(client, admin_headers):
    response = client.post(
        "/api/v1/captures/does-not-exist/teach", headers=admin_headers, json={"plate": "MH12AB3457"}
    )
    assert response.status_code == 404


def test_learning_stats_endpoint(client, admin_headers, viewer_headers):
    # The session-scoped client carries the login cookie, so read access is
    # exercised with the admin token and writes are checked by role.
    stats = client.get("/api/v1/learning", headers=admin_headers)
    assert stats.status_code == 200
    assert "exemplars" in stats.json()

    assert client.delete("/api/v1/learning", headers=viewer_headers).status_code == 403
    assert client.delete("/api/v1/learning", headers=admin_headers).status_code == 204
