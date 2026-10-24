# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# PlateVision — production image
# Multi-stage build: wheels are compiled on a builder layer, the runtime layer
# only receives the wheels + app, keeping the final image slim and free of
# compilers (important for security scanning and cold-start on the edge).
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

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

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH" \
    ENVIRONMENT=production \
    PORT=8000

# OpenCV's headless wheel still needs these two shared libraries at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 platevision \
    && useradd --system --uid 1001 --gid platevision --home /app platevision

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=platevision:platevision app ./app
COPY --chown=platevision:platevision scripts ./scripts
COPY --chown=platevision:platevision docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /app/storage/uploads /app/storage/models \
    && chown -R platevision:platevision /app/storage

USER platevision
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY:-2} --proxy-headers --forwarded-allow-ips='*'"]
