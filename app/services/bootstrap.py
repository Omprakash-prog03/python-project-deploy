"""Boot-time preparation: schema, demo seeding and engine warm-up.

This logic used to live in ``scripts/init_db.py`` and run to completion *before*
uvicorn started. That ordering is what killed deploys: seeding 34 demo frames
meant 34 real OCR passes, 17-34 s each on a free-tier CPU, so the process spent
a quarter of an hour working before it ever bound a port — and Render reports
that as "no open ports detected" and kills the deploy.

Two changes fix it, and both live here:

* the demo readings are replayed from a frozen cache (see
  :mod:`app.services.seed_cache`), so seeding is a few seconds of image
  rendering rather than minutes of OCR;
* :func:`start_background_bootstrap` runs the remaining work on a daemon thread
  *after* the server is accepting connections, so no amount of preparation can
  ever block the port again.

The schema is still created synchronously — a request must never see missing
tables. Everything else can happen while the server is already answering.
"""
from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import Base, SessionLocal, engine
from app.models import entities  # noqa: F401  (register metadata)

logger = get_logger("bootstrap")

# Arbitrary but stable key: every PlateVision process agrees on it.
ADVISORY_LOCK_KEY = 0x50565F31  # "PV_1"


@contextlib.contextmanager
def schema_lock() -> Iterator[None]:
    """Serialise schema creation and seeding across processes and replicas.

    Several replicas booting at once would otherwise race inside
    ``Base.metadata.create_all`` (each inspects, both CREATE, one fails) and
    could create duplicate demo tenants. Postgres gets a session advisory lock;
    SQLite gets an ``flock`` on a sidecar file next to the database.
    """
    if engine.dialect.name == "postgresql":
        with engine.connect() as connection:
            connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY})
            try:
                yield
            finally:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY}
                )
        return

    if engine.dialect.name == "sqlite":
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX development hosts
            yield
            return
        db_path = settings.database_url.split("///")[-1]
        lock_path = Path(db_path).with_suffix(".init.lock") if db_path not in ("", ":memory:") else None
        if lock_path is None:
            yield
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return

    yield


def create_schema(*, quiet: bool = False) -> None:
    """Create missing tables, tolerating a concurrent creator."""
    try:
        Base.metadata.create_all(bind=engine)
    except ProgrammingError as exc:  # pragma: no cover - only on racing boot
        if "already exists" not in str(exc).lower():
            raise
        if not quiet:
            logger.info("Another process created the schema first; continuing")


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
            logger.info("Waiting for database (attempt %d) …", attempt)
            time.sleep(interval)


def seed_demo_workspace(*, force: bool = False) -> bool:
    """Provision the demo tenant if the database has no organisation yet."""
    if not (force or settings.seed_demo_data):
        logger.info("Demo seeding disabled — starting with an empty tenant list")
        return False
    from app.seed import seed_if_empty

    started = time.perf_counter()
    with schema_lock(), SessionLocal() as db:
        created = seed_if_empty(db)
    elapsed = time.perf_counter() - started
    if created:
        logger.info("Demo workspace provisioned in %.1fs", elapsed)
    else:
        logger.info("Demo workspace already present")
    return created


def warm_engine() -> None:
    """Build the glyph bank up front so the first real scan is not penalised."""
    try:
        from app.alpr.ocr import get_classifier

        started = time.perf_counter()
        get_classifier()._ensure()
        logger.info("Glyph bank ready in %.1fs", time.perf_counter() - started)
    except Exception:  # pragma: no cover - non-fatal for boot
        logger.warning("Glyph bank warm-up failed; it will build on first use", exc_info=True)


def prepare(
    *,
    seed: bool = True,
    warm: bool = True,
    timeout: float | None = 45.0,
) -> None:
    """Run the whole preparation sequence synchronously."""
    if timeout is not None:
        wait_for_database(timeout)
    with schema_lock():
        create_schema()
        logger.info("Schema ready (%d tables)", len(Base.metadata.tables))
    if seed:
        seed_demo_workspace()
    if warm:
        warm_engine()


def start_background_bootstrap(*, seed: bool = True, warm: bool = True) -> threading.Thread:
    """Prepare seeding and the engine on a daemon thread, after the port is up.

    Returns the thread so tests can join it. Failures are logged and swallowed:
    a demo without fixtures is a degraded demo, not a dead service.
    """

    def _run() -> None:
        try:
            if seed:
                seed_demo_workspace()
        except Exception:
            logger.warning("Demo seeding failed; the workspace may be empty", exc_info=True)
        try:
            if warm:
                warm_engine()
        except Exception:  # pragma: no cover - defensive
            logger.warning("Engine warm-up failed", exc_info=True)

    thread = threading.Thread(target=_run, name="pv-bootstrap", daemon=True)
    thread.start()
    return thread
