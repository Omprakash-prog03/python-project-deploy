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
    rate_limit_per_minute: int = 240
    rate_limit_burst: int = 60
    trust_proxy_headers: bool = True

    # ------------------------------------------------------------------ jobs
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
        """Create runtime directories eagerly."""
        for directory in (self.storage_dir, self.model_cache_dir):
            directory.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite"):
            db_path = self.database_url.split("///")[-1]
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # Production hardening guards.
        if self.is_production and "dev-only" in self.secret_key:
            raise RuntimeError(
                "SECRET_KEY must be set to a strong random value in production."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
