#!/bin/sh
# Container entrypoint: fail fast on bad config, then hand over to the server.
set -e

echo "[entrypoint] environment=${ENVIRONMENT:-development} port=${PORT:-8000}"

if [ "${ENVIRONMENT}" = "production" ]; then
  if [ -z "${SECRET_KEY}" ] || [ "${#SECRET_KEY}" -lt 32 ]; then
    echo "[entrypoint] FATAL: SECRET_KEY must be set to at least 32 characters in production." >&2
    exit 1
  fi
  if [ -z "${DATABASE_URL}" ]; then
    echo "[entrypoint] WARNING: DATABASE_URL not set, using bundled SQLite." >&2
  fi
fi

mkdir -p "${STORAGE_DIR:-/app/storage/uploads}" "${MODEL_CACHE_DIR:-/app/storage/models}"

exec "$@"
