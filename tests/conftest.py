"""Test harness: an isolated SQLite workspace and authenticated client."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Configure the app *before* it is imported anywhere else.
_TMP = Path(tempfile.mkdtemp(prefix="platevision-tests-"))
os.environ.update(
    ENVIRONMENT="test",
    SECRET_KEY="test-secret-key-that-is-long-enough-for-hs256",
    DATABASE_URL=f"sqlite:///{_TMP / 'test.db'}",
    STORAGE_DIR=str(_TMP / "uploads"),
    MODEL_CACHE_DIR=str(_TMP / "models"),
    SEED_DEMO_DATA="false",
    ALLOW_DEMO_LOGIN="true",
    RATE_LIMIT_PER_MINUTE="100000",
    RATE_LIMIT_BURST="10000",
    ALPR_DEFAULT_COUNTRY="IN",
)

from fastapi.testclient import TestClient  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.db.session import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.entities import Camera, Organization, User, WatchlistEntry  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def workspace(client):
    """Create a tenant with one admin, one viewer and a camera."""
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        org = Organization(name="Test Authority", slug="test", plan="enterprise")
        db.add(org)
        db.flush()
        admin = User(
            org_id=org.id,
            email="admin@test.io",
            full_name="Test Admin",
            role="admin",
            password_hash=hash_password("test-password"),
            avatar_initials="TA",
        )
        viewer = User(
            org_id=org.id,
            email="viewer@test.io",
            full_name="Test Viewer",
            role="viewer",
            password_hash=hash_password("test-password"),
            avatar_initials="TV",
        )
        camera = Camera(org_id=org.id, code="CAM-1", name="Gate One", corridor="Test Corridor")
        db.add_all([admin, viewer, camera])
        db.flush()
        db.add(
            WatchlistEntry(
                org_id=org.id,
                plate="MH12AB3456",
                label="Stolen",
                category="stolen",
                severity="critical",
                reason="test",
            )
        )
        db.commit()
        return {"org_id": org.id, "camera_id": camera.id, "admin_id": admin.id, "viewer_id": viewer.id}


@pytest.fixture(scope="session")
def admin_headers(client, workspace):
    response = client.post(
        "/api/v1/auth/login", json={"email": "admin@test.io", "password": "test-password"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture(scope="session")
def viewer_headers(client, workspace):
    response = client.post(
        "/api/v1/auth/login", json={"email": "viewer@test.io", "password": "test-password"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
