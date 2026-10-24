"""Idempotent schema bootstrap and optional demo seeding.

Run this once per deployment *before* the web workers start:

    python scripts/init_db.py            # create tables, no demo data
    python scripts/init_db.py --seed     # also provision the demo workspace

Running it from the entrypoint (rather than inside each worker's lifespan)
avoids concurrent ``CREATE TABLE`` races on SQLite and keeps startup time off
the request path. The script is safe to run repeatedly and concurrently.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.session import Base, SessionLocal, engine
from app.models import entities  # noqa: F401  (register metadata)


def wait_for_database(timeout: float = 45.0, interval: float = 1.5) -> None:
    """Block until the database accepts connections (compose/k8s friendly)."""
    deadline = time.time() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return
        except OperationalError as exc:
            if time.time() >= deadline:
                raise RuntimeError(
                    f"Database not reachable after {timeout:.0f}s: {exc}"
                ) from exc
            print(f"[init_db] waiting for database (attempt {attempt}) …", flush=True)
            time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the PlateVision schema")
    parser.add_argument("--seed", action="store_true", help="provision demo data if empty")
    parser.add_argument("--timeout", type=float, default=45.0, help="database wait timeout")
    args = parser.parse_args()

    configure_logging()
    logger = get_logger("init_db")
    logger.info(
        "Bootstrapping %s (%s backend, storage=%s)",
        settings.app_name,
        settings.backend_kind,
        settings.storage_backend,
    )

    wait_for_database(args.timeout)
    Base.metadata.create_all(bind=engine)
    logger.info("Schema ready (%d tables)", len(Base.metadata.tables))

    seed_requested = args.seed or settings.seed_demo_data
    if seed_requested:
        from app.seed import seed_if_empty

        with SessionLocal() as db:
            created = seed_if_empty(db)
        logger.info("Demo workspace %s", "created" if created else "already present")
    else:
        logger.info("Demo seeding disabled — starting with an empty tenant list")

    # Warm the glyph bank so the first recognition request is not penalised.
    try:
        from app.alpr.ocr import get_classifier

        get_classifier()._ensure()
        logger.info("Glyph bank ready")
    except Exception:  # pragma: no cover - non-fatal for boot
        logger.warning("Glyph bank warm-up failed; it will build on first use", exc_info=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
