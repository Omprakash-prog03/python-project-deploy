"""Deployment-artifact tests.

These cover the things that silently break a real deploy rather than the API
surface: configuration guards, container boot scripts, manifests and headers.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ manifests
def test_manifests_parse():
    for name in ("compose.yaml", "render.yaml"):
        assert yaml.safe_load((ROOT / name).read_text()), name
    assert json.loads((ROOT / "railway.json").read_text())
    try:
        import tomllib

        assert tomllib.loads((ROOT / "fly.toml").read_text())["app"]
    except ImportError:  # pragma: no cover - python < 3.11
        pass


def test_kubernetes_manifests_parse():
    docs = [d for d in yaml.safe_load_all((ROOT / "k8s/platevision.yaml").read_text()) if d]
    kinds = {d["kind"] for d in docs}
    assert {"Deployment", "Service", "Ingress", "Job"} <= kinds
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    # Probes must exist and point at real endpoints.
    assert container["readinessProbe"]["httpGet"]["path"] == "/api/v1/health"
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"


def test_dockerfile_contract():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "USER platevision" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "BOOTSTRAP_ON_START=false" in dockerfile
    # The image must not bake in a secret.
    assert "SECRET_KEY=" not in dockerfile
    for path in ("app", "scripts", "docker/entrypoint.sh"):
        assert path in dockerfile


def test_dockerignore_excludes_secrets_and_data():
    patterns = (ROOT / ".dockerignore").read_text().split()
    for pattern in ("storage", ".env", ".git", "tests"):
        assert pattern in patterns


def test_entrypoint_is_valid_posix_shell():
    result = subprocess.run(
        ["sh", "-n", str(ROOT / "docker/entrypoint.sh")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    script = (ROOT / "docker/entrypoint.sh").read_text()
    assert "exit 1" in script  # fails fast without a production secret
    assert 'exec "$@"' in script  # signals reach uvicorn as PID 1


def test_postgres_driver_is_a_declared_dependency():
    requirements = (ROOT / "requirements.txt").read_text()
    assert "psycopg" in requirements, "compose/fly default to Postgres; ship the driver"


def test_compose_defaults_are_coherent():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    env = compose["services"]["api"]["environment"]
    assert "postgresql+psycopg" in str(env["DATABASE_URL"])
    assert env["BOOTSTRAP_ON_START"] == "false"
    assert compose["services"]["db"]["healthcheck"]


def test_static_bundle_has_no_demo_credentials():
    """The console must never ship a working password in plain text."""
    html = (ROOT / "app/static/index.html").read_text()
    js = (ROOT / "app/static/js/app.js").read_text()
    for needle in ('value="nightwatch"', "value=\"elara@platevision.io\""):
        assert needle not in html
    assert "'nightwatch'" not in js and '"nightwatch"' not in js


# ----------------------------------------------------------- config hardening
def _settings(**env):
    """Build a Settings instance from an isolated environment."""
    from app.core.config import Settings

    base = {
        "ENVIRONMENT": "production",
        "SECRET_KEY": "x" * 40,
        "DATABASE_URL": "sqlite:////tmp/pv-config-test.db",
        "STORAGE_DIR": "/tmp/pv-config-test/uploads",
        "MODEL_CACHE_DIR": "/tmp/pv-config-test/models",
    }
    base.update(env)
    keys = [
        "ENVIRONMENT", "SECRET_KEY", "DATABASE_URL", "STORAGE_DIR", "MODEL_CACHE_DIR",
        "COOKIE_SECURE", "CORS_ORIGINS", "ALLOWED_HOSTS", "DEBUG", "STORAGE_BACKEND",
        "S3_BUCKET", "ALLOW_DEMO_LOGIN", "SEED_DEMO_DATA",
    ]
    saved = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        os.environ.update(base)
        return Settings(_env_file=None)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_production_requires_a_strong_secret():
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _settings(SECRET_KEY="short")


def test_production_rejects_the_dev_default_secret():
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _settings(SECRET_KEY="dev-only-change-me-6f1c9a2e8b4d7f0a3c5e9b1d")


def test_production_enables_secure_cookies_by_default():
    assert _settings().cookie_secure is True
    assert _settings(COOKIE_SECURE="false").cookie_secure is False


def test_production_rejects_s3_without_a_bucket():
    with pytest.raises(RuntimeError, match="S3_BUCKET"):
        _settings(STORAGE_BACKEND="s3")


def test_wildcard_cors_never_claims_credentials():
    wildcard = _settings()
    assert wildcard.cors_origin_list == ["*"]
    assert wildcard.cors_allow_credentials is False

    explicit = _settings(CORS_ORIGINS="https://plates.example.com")
    assert explicit.cors_allow_credentials is True


def test_allowed_hosts_parsing():
    assert _settings().allowed_host_list == ["*"]
    parsed = _settings(ALLOWED_HOSTS="plates.example.com, platevision.internal")
    assert parsed.allowed_host_list == ["plates.example.com", "platevision.internal"]


def test_development_allows_relaxed_defaults():
    relaxed = _settings(ENVIRONMENT="development", SECRET_KEY="dev")
    assert relaxed.is_production is False


# ------------------------------------------------------ container boot smoke
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def booted_server(tmp_path_factory):
    """Boot the real app the way the container does: init_db, then uvicorn."""
    tmp = tmp_path_factory.mktemp("boot")
    env = {
        **os.environ,
        "ENVIRONMENT": "production",
        "SECRET_KEY": "boot-test-secret-key-0123456789abcdef",
        "DATABASE_URL": f"sqlite:///{tmp}/boot.db",
        "STORAGE_DIR": str(tmp / "uploads"),
        "MODEL_CACHE_DIR": str(tmp / "models"),
        "BOOTSTRAP_ON_START": "false",
        "SEED_DEMO_DATA": "true",
        "ALLOW_DEMO_LOGIN": "true",
        "ALLOWED_HOSTS": "plates.example.com,127.0.0.1",
        "COOKIE_SECURE": "true",
    }
    init = subprocess.run(
        [sys.executable, "scripts/init_db.py"], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert init.returncode == 0, init.stderr[-2000:]

    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--workers", "2", "--proxy-headers"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 90
    try:
        while time.time() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"server exited:\n{process.stdout.read()[-3000:]}")
            try:
                if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                    break
            except Exception:
                time.sleep(0.4)
        else:  # pragma: no cover
            raise AssertionError("server did not become healthy")
        yield base, process
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


def test_container_boot_serves_console_and_api(booted_server):
    base, _ = booted_server
    with httpx.Client(base_url=base, timeout=30.0) as client:
        page = client.get("/", headers={"host": "plates.example.com"})
        assert page.status_code == 200 and "PlateVision" in page.text
        assert page.headers["strict-transport-security"]
        assert page.headers["cache-control"] == "no-cache"

        asset = client.get("/static/css/app.css")
        assert asset.headers["cache-control"].startswith("public")

        config = client.get("/api/v1/config").json()
        assert config["app_name"] == "PlateVision"
        assert "secret" not in json.dumps(config).lower()

        login = client.post(
            "/api/v1/auth/login",
            json={"email": "elara@platevision.io", "password": "nightwatch"},
            headers={"host": "plates.example.com"},
        )
        assert login.status_code == 200
        cookie = login.headers.get("set-cookie", "")
        assert "HttpOnly" in cookie and "Secure" in cookie

        health = client.get("/api/v1/health", headers={"host": "plates.example.com"}).json()
        assert health["status"] == "ok" and health["environment"] == "production"


def test_trusted_host_rejects_unknown_hosts(booted_server):
    base, _ = booted_server
    response = httpx.get(f"{base}/api/v1/config", headers={"host": "evil.example.net"}, timeout=10)
    assert response.status_code == 400
