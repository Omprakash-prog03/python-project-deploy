#!/bin/sh
# Container entrypoint: validate configuration, prepare the database once, then
# hand over to the process manager. Keeping schema creation here (single
# process, before the workers fork) removes both the SQLite CREATE TABLE race
# and cold-start latency from the first request.
set -e

log() { echo "[entrypoint] $*"; }

log "environment=${ENVIRONMENT:-development} port=${PORT:-8000} workers=${WEB_CONCURRENCY:-2}"

if [ "${ENVIRONMENT}" = "production" ]; then
  if [ -z "${SECRET_KEY}" ] || [ "${#SECRET_KEY}" -lt 32 ]; then
    log "FATAL: SECRET_KEY must be set to at least 32 characters in production." >&2
    log "       Generate one with: openssl rand -hex 32" >&2
    exit 1
  fi
fi

if [ -z "${DATABASE_URL}" ]; then
  log "WARNING: DATABASE_URL not set — falling back to bundled SQLite."
fi

mkdir -p "${STORAGE_DIR:-/app/storage/uploads}" "${MODEL_CACHE_DIR:-/app/storage/models}"

# SQLite allows exactly one writer; multiple uvicorn workers contend on the WAL
# and surface "database is locked" under load. Force a single worker and let the
# operator scale with replicas (or move to Postgres) instead of failing in prod.
case "${DATABASE_URL:-sqlite}" in
  sqlite*|"")
    case "${WEB_CONCURRENCY:-}" in
      ""|1) ;;
      *) log "SQLite backend detected — overriding WEB_CONCURRENCY=${WEB_CONCURRENCY} with 1." ;;
    esac
    WEB_CONCURRENCY=1
    export WEB_CONCURRENCY
    ;;
esac

# `init` / `shell` subcommands for one-off jobs and debugging.
case "${1:-}" in
  init)  exec python scripts/init_db.py ;;
  seed)  exec python scripts/init_db.py --seed ;;
  shell) exec python ;;
esac

log "Preparing database …"
# init_db.py honours SEED_DEMO_DATA itself, so no flag juggling here.
python scripts/init_db.py

log "Starting application …"
exec "$@"
