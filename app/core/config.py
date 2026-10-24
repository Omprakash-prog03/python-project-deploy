"""Application configuration.

All runtime configuration is environment driven so the same image can be
deployed to Docker, Fly.io, Render, Railway or a bare VM without code changes.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent


class Settings(BaseSettings):
    """Central settings object, loaded once and cached."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    app_name: str = "PlateVision"
    app_tagline: str = "Plate Intelligence Platform"
    version: str = "1.0.0"
    environment: Literal["development", "staging", "production", "test"] = "development"
    debug: bool = False
    root_path: str = ""

    # ------------------------------------------------------------------ auth
    secret_key: str = "dev-only-change-me-6f1c9a2e8b4d7f0a3c5e9b1d"
    access_token_ttl_minutes: int = 60 * 12
    refresh_token_ttl_days: int = 14
    cookie_secure: bool = False
    cookie_domain: str | None = None
    allow_demo_login: bool = True
    demo_email: str = "elara@platevision.io"
    demo_password: str = "nightwatch"

    # ------------------------------------------------------------------ data
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'storage' / 'platevision.db'}"
    storage_backend: Literal["local", "s3"] = "local"
    storage_dir: Path = PROJECT_ROOT / "storage" / "uploads"
    storage_public_base: str = "/api/v1/captures"
    s3_bucket: str | None = None
    s3_region: str = "ap-south-1"
    s3_endpoint_url: str | None = None

    model_cache_dir: Path = PROJECT_ROOT / "storage" / "models"

    # ------------------------------------------------------------------ alpr
    alpr_min_confidence: float = 0.42
    alpr_review_confidence: float = 0.72
    alpr_max_image_dim: int = 1800
    alpr_max_candidates: int = 6
    alpr_countries: str = "IN,US,UK,EU,GENERIC"
    alpr_default_country: str = "IN"
    alpr_ocr_engine: Literal["hybrid", "template", "knn"] = "hybrid"
    alpr_duplicate_window_seconds: int = 90
    alpr_watchlist_threshold: float = 0.86

    # ------------------------------------------------------------------ http
    max_upload_mb: int = 12
    cors_origins: str = "*"
    allowed_hosts: str = "*"
    rate_limit_per_minute: int = 240
    rate_limit_burst: int = 60
    trust_proxy_headers: bool = True

    # ------------------------------------------------------------------ jobs
    # Containers prepare the schema once via scripts/init_db.py before workers
    # fork; set BOOTSTRAP_ON_START=false there to avoid duplicated work and
    # concurrent CREATE TABLE attempts. Bare `uvicorn` keeps it on by default.
    bootstrap_on_start: bool = True
    seed_demo_data: bool = True
    webhook_timeout_seconds: float = 5.0
    webhook_max_attempts: int = 3

    # ------------------------------------------------------------------ derived
    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def cors_allow_credentials(self) -> bool:
        """Wildcard origins and credentialed requests are mutually exclusive.

        The console is served from the same origin, so cookies never need CORS.
        If an operator wants a cross-origin console (cookies included) they must
        list explicit origins; otherwise we correctly advertise ``*`` without
        credentials, which is what browsers actually accept.
        """
        return self.cors_origin_list != ["*"]

    @property
    def allowed_host_list(self) -> list[str]:
        if self.allowed_hosts.strip() == "*":
            return ["*"]
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def backend_kind(self) -> str:
        return "postgresql" if self.database_url.startswith("postgresql") else "sqlite"

    @property
    def country_list(self) -> list[str]:
        return [c.strip().upper() for c in self.alpr_countries.split(",") if c.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @field_validator("storage_dir", "model_cache_dir", mode="before")
    @classmethod
    def _expand_path(cls, v: str | Path) -> Path:
        return Path(str(v)).expanduser().resolve()

    def model_post_init(self, __context: object) -> None:
        """Create runtime directories eagerly and enforce production policy."""
        for directory in (self.storage_dir, self.model_cache_dir):
            directory.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite"):
            db_path = self.database_url.split("///")[-1]
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        if not self.is_production:
            return

        # ------------------------------------------------------- hard failures
        if "dev-only" in self.secret_key or len(self.secret_key) < 32:
            raise RuntimeError(
                "SECRET_KEY must be set to a strong random value (>= 32 chars) in "
                "production. Generate one with: openssl rand -hex 32"
            )
        if self.storage_backend == "s3" and not self.s3_bucket:
            raise RuntimeError("STORAGE_BACKEND=s3 requires S3_BUCKET to be set.")
        if self.debug:
            raise RuntimeError("DEBUG must be false in production.")

        # -------------------------------------------------- safe auto-defaults
        # Cookies must be Secure behind TLS unless explicitly overridden; a
        # plain-HTTP deployment can still opt out by setting COOKIE_SECURE=false.
        if "cookie_secure" not in self.model_fields_set:
            object.__setattr__(self, "cookie_secure", True)

        # --------------------------------------------------------- advisories
        if self.allow_demo_login:
            _warn(
                "ALLOW_DEMO_LOGIN is enabled in production — anyone can sign in "
                "with the demo account. Set ALLOW_DEMO_LOGIN=false."
            )
        if self.seed_demo_data:
            _warn(
                "SEED_DEMO_DATA is enabled in production — a demo tenant will be "
                "created on boot. Set SEED_DEMO_DATA=false."
            )
        if self.cors_origin_list == ["*"]:
            _warn(
                "CORS_ORIGINS=* in production. The API is reachable from any web "
                "origin; restrict it to your console origin."
            )
        if self.allowed_host_list == ["*"]:
            _warn(
                "ALLOWED_HOSTS=* in production — Host headers are not validated. "
                "Set ALLOWED_HOSTS to your domains (e.g. plates.example.com)."
            )
        if not self.cookie_secure:
            _warn("COOKIE_SECURE=false in production — session cookies will travel over HTTP.")
        if self.backend_kind == "sqlite":
            _warn(
                "Using SQLite in production. Fine for a single instance, but set "
                "DATABASE_URL to Postgres before running more than one replica."
            )

    @property
    def warnings(self) -> list[str]:
        """Non-fatal configuration advisories, surfaced on /api/v1/config."""
        items: list[str] = []
        if self.allow_demo_login:
            items.append("demo_login_enabled")
        if self.seed_demo_data:
            items.append("demo_data_seeding")
        if self.cors_origin_list == ["*"]:
            items.append("wildcard_cors")
        return items


def _warn(message: str) -> None:
    """Emit a configuration warning without importing the logger (cycle-safe)."""
    import logging

    logging.getLogger("platevision.config").warning(message)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
