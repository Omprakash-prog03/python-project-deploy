"""Capture storage abstraction (local disk or S3-compatible object storage)."""
from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


class StorageService:
    """Writes capture images with a date-partitioned key layout."""

    def __init__(self) -> None:
        self.backend = settings.storage_backend
        self.root = settings.storage_dir
        self.root.mkdir(parents=True, exist_ok=True)
        if self.backend == "s3":
            try:
                import boto3  # type: ignore

                self._client = boto3.client(
                    "s3",
                    region_name=settings.s3_region,
                    endpoint_url=settings.s3_endpoint_url,
                )
                self._verify_bucket()
            except Exception as exc:  # pragma: no cover - depends on infrastructure
                # Silently writing evidence to a container's ephemeral disk in
                # production would lose captures on the next deploy, so this is
                # a hard failure there and a loud fallback in development.
                if settings.is_production:
                    raise RuntimeError(
                        "STORAGE_BACKEND=s3 is configured but object storage is "
                        f"unreachable: {exc}. Install the extra (pip install -r "
                        "requirements-s3.txt or build with --build-arg INSTALL_S3=1) "
                        "and check the bucket/credentials."
                    ) from exc
                logger.error("S3 backend unavailable, falling back to local disk: %s", exc)
                self.backend = "local"
                self._client = None
        else:
            self._client = None

    def _verify_bucket(self) -> None:  # pragma: no cover - network dependency
        assert self._client is not None
        self._client.head_bucket(Bucket=settings.s3_bucket)

    # ------------------------------------------------------------------ write
    def save(self, data: bytes, *, org_id: str, suffix: str = ".jpg") -> tuple[str, int]:
        suffix = suffix.lower() if suffix.lower() in ALLOWED_EXTENSIONS else ".jpg"
        digest = hashlib.sha256(data).hexdigest()[:20]
        stamp = datetime.now(UTC)
        return self.save_key(data, key=self._key(org_id, stamp, digest, suffix))

    def save_key(self, data: bytes, *, key: str) -> tuple[str, int]:
        if self.backend == "s3" and self._client is not None:  # pragma: no cover
            self._client.put_object(
                Bucket=settings.s3_bucket,
                Key=key,
                Body=data,
                ContentType="image/jpeg",
                CacheControl="private, max-age=86400",
            )
            return key, len(data)
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return key, len(data)

    def write_derived(self, key: str, data: bytes) -> str:
        return self.save_key(data, key=key)[0]

    # ------------------------------------------------------------------- read
    def read(self, key: str) -> bytes:
        if key.startswith("http://") or key.startswith("https://"):
            raise AppError("Remote capture references are not readable.", code="invalid_storage_key")
        if self.backend == "s3" and self._client is not None:  # pragma: no cover
            try:
                obj = self._client.get_object(Bucket=settings.s3_bucket, Key=key)
                return obj["Body"].read()
            except Exception as exc:
                raise AppError("Capture not found in object storage.", status_code=404, code="not_found") from exc
        path = self._safe_path(key)
        if not path.exists():
            raise AppError("Capture image is missing from storage.", status_code=404, code="not_found")
        return path.read_bytes()

    def delete(self, key: str) -> None:
        try:
            if self.backend == "s3" and self._client is not None:  # pragma: no cover
                self._client.delete_object(Bucket=settings.s3_bucket, Key=key)
                return
            path = self._safe_path(key)
            path.unlink(missing_ok=True)
        except Exception:  # pragma: no cover
            logger.warning("Failed to delete capture %s", key, exc_info=True)

    # ------------------------------------------------------------------ utils
    def _safe_path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if not str(candidate).startswith(str(self.root.resolve())):
            raise AppError("Invalid storage key.", code="invalid_storage_key")
        return candidate

    @staticmethod
    def _key(org_id: str, stamp: datetime, digest: str, suffix: str) -> str:
        return f"{org_id}/{stamp:%Y/%m/%d}/{stamp:%H%M%S}-{digest}{suffix}"

    def disk_usage_mb(self) -> float:
        if self.backend != "local":
            return 0.0
        total = sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())
        return round(total / (1024 * 1024), 2)

    def purge_org(self, org_id: str) -> None:
        if self.backend == "local":
            shutil.rmtree(self.root / org_id, ignore_errors=True)


storage = StorageService()
