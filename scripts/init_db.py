"""Idempotent schema bootstrap and optional demo seeding.

The container entrypoint runs this once, before the web workers start, to create
tables without the demo fixtures::

    python scripts/init_db.py --schema-only   # tables only, fast
    python scripts/init_db.py --seed          # also provision the demo workspace
    python scripts/init_db.py                 # tables, plus demo data if enabled

Seeding is *not* run from the entrypoint by default. The demo workspace is
provisioned by the app's own background bootstrap once the port is bound, so a
slow seed can never make a deploy time out. ``--seed`` remains for one-off jobs
and local setup.

The implementation lives in :mod:`app.services.bootstrap` so the script and the
application cannot drift apart.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.session import Base
from app.services import bootstrap


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the PlateVision schema")
    parser.add_argument("--seed", action="store_true", help="provision demo data if empty")
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="create tables and stop; the app seeds itself once it is serving",
    )
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

    bootstrap.wait_for_database(args.timeout)

    if args.schema_only:
        with bootstrap.schema_lock():
            bootstrap.create_schema()
            logger.info("Schema ready (%d tables)", len(Base.metadata.tables))
        logger.info("Schema-only run complete; the app will seed itself when it serves")
        return 0

    # `--seed` forces seeding even when SEED_DEMO_DATA is off; a bare run obeys
    # the setting, which is what local `make init` expects.
    if args.seed or settings.seed_demo_data:
        bootstrap.prepare(seed=True, warm=True, timeout=None)
    else:
        with bootstrap.schema_lock():
            bootstrap.create_schema()
            logger.info("Schema ready (%d tables)", len(Base.metadata.tables))
        logger.info("Demo seeding disabled — starting with an empty tenant list")
        bootstrap.warm_engine()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
