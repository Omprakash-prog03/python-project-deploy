# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# PlateVision — production image
#
# Two stages: wheels are built with a toolchain, the runtime layer receives only
# the virtualenv. The final image has no compilers, runs as an unprivileged user
# and prepares its schema exactly once before the web workers fork.
#
#   docker build -t platevision/api:1.0.0 .
#   docker build --build-arg INSTALL_S3=1 -t platevision/api:1.0.0-s3 .
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# libgl/libglib are needed to *link* opencv-python-headless wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libglib2.0-0 \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install -r requirements.txt

# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Schema is prepared once by the entrypoint, before workers fork.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH" \
    ENVIRONMENT=production \
    BOOTSTRAP_ON_START=false \
    PORT=8000

# OpenCV's headless wheel still needs these shared libraries at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 platevision \
    && useradd --system --uid 1001 --gid platevision --home /app platevision

COPY --from=builder /opt/venv /opt/venv

# Optional object storage (S3 / R2 / MinIO) — ~12 MB, skipped by default.
ARG INSTALL_S3=0
RUN if [ "$INSTALL_S3" = "1" ]; then \
        /opt/venv/bin/pip install --no-cache-dir "boto3==1.35.90"; \
    fi

WORKDIR /app
COPY --chown=platevision:platevision app ./app
COPY --chown=platevision:platevision scripts ./scripts
COPY --chown=platevision:platevision docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /app/storage/uploads /app/storage/models \
    && chown -R platevision:platevision /app/storage

USER platevision
EXPOSE 8000

# Liveness only: never restart a container because a dependency hiccuped.
# Readiness (database + storage + engine) is /api/v1/health for orchestrators.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY:-2} --proxy-headers --forwarded-allow-ips='*'"]
