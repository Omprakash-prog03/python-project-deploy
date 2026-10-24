# Deployment guide

PlateVision is a single stateless web service with two stateful dependencies: a database and
an evidence store. Everything else (fonts, glyph bank sources, console, imagery) ships in the
image.

## 1. Build the image

```bash
docker build -t platevision/api:1.0.0 .
docker run --rm -p 8000:8000 \
  -e ENVIRONMENT=production \
  -e SECRET_KEY=$(openssl rand -hex 32) \
  -e DATABASE_URL=sqlite:////data/platevision.db \
  -e STORAGE_DIR=/data/uploads \
  -e MODEL_CACHE_DIR=/data/models \
  -v platevision:/data \
  platevision/api:1.0.0
```

The container runs as uid 1001 (`platevision`), has a healthcheck on `/healthz` and refuses to
start in production without a ≥32 character `SECRET_KEY`.

## 2. Compose (app + Postgres)

```bash
cp .env.example .env            # set SECRET_KEY, POSTGRES_PASSWORD
docker compose up -d --build
docker compose exec api python -m app.seed   # optional demo data
```

Volumes: `captures` (evidence), `models` (glyph-bank cache), `pgdata`.

## 3. Fly.io

```bash
fly launch --no-deploy                 # keeps the provided fly.toml
fly volumes create platevision_data --size 5
fly secrets set SECRET_KEY=$(openssl rand -hex 32)
fly deploy
```

`fly.toml` pins `bom` (Mumbai) — change `primary_region` to keep data in-country.

## 4. Render

Create a **Blueprint** from the repo; `render.yaml` provisions the web service, a 5 GB disk
mounted at `/var/data`, a generated `SECRET_KEY` and `COOKIE_SECURE=true`.

## 5. Railway

`railway.json` selects the Dockerfile builder, sets the start command and health check.
Add a volume mounted at `/data` and set:

```
SECRET_KEY=<32+ chars>
DATABASE_URL=sqlite:////data/platevision.db
STORAGE_DIR=/data/uploads
MODEL_CACHE_DIR=/data/models
SEED_DEMO_DATA=false
```

## 6. Bare metal / systemd

```ini
# /etc/systemd/system/platevision.service
[Unit]
Description=PlateVision
After=network.target

[Service]
User=platevision
WorkingDirectory=/opt/platevision
EnvironmentFile=/etc/platevision.env
ExecStart=/opt/platevision/.venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 --workers 2 --proxy-headers
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/platevision

[Install]
WantedBy=multi-user.target
```

```nginx
server {
  listen 443 ssl http2;
  server_name plates.example.com;
  client_max_body_size 16m;

  location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
  }
}
```

## Production checklist

- [ ] `SECRET_KEY` from a secret manager (≥32 chars) — the app refuses to boot otherwise
- [ ] `ENVIRONMENT=production` → JSON logs, HSTS, stricter config validation
- [ ] `COOKIE_SECURE=true` and TLS terminated in front of the app
- [ ] `CORS_ORIGINS` restricted to your console origin
- [ ] `ALLOW_DEMO_LOGIN=false`, `SEED_DEMO_DATA=false` for a clean tenant
      (then create your organisation and first admin — see below)
- [ ] `DATABASE_URL` pointing at Postgres with automated backups
- [ ] Persistent volume for `STORAGE_DIR` (+ `MODEL_CACHE_DIR` to skip the 3 s bank build)
- [ ] Object storage (`STORAGE_BACKEND=s3`) for multi-replica deployments — local disk is
      per-replica and will 404 evidence served by a different instance
- [ ] Load balancer health check on `/healthz` (liveness) and `/api/v1/health` (readiness
      with database/storage/engine detail)
- [ ] Scrape `/api/v1/metrics` with Prometheus
- [ ] Log shipping: every line is JSON in production and carries `request_id`
- [ ] Alert on `platevision_health_ok == 0`, 5xx rate, and `capture.created` silence
      (a dead corridor looks exactly like a quiet one)
- [ ] Evidence retention: schedule `DELETE /api/v1/captures/{id}` or prune the volume per policy
- [ ] Calibrate `ALPR_REVIEW_CONFIDENCE` against your own corpus before enabling auto-clearance

## First admin on a clean deployment

```bash
python - <<'PY'
from app.db.session import Base, SessionLocal, engine
from app.models.entities import Organization, User
from app.core.security import hash_password

Base.metadata.create_all(bind=engine)
with SessionLocal() as db:
    org = Organization(name="Your Authority", slug="your-authority")
    db.add(org); db.flush()
    db.add(User(org_id=org.id, email="ops@your-authority.gov",
                full_name="Ops Lead", role="admin", avatar_initials="OL",
                password_hash=hash_password("CHANGE-ME-IMMEDIATELY")))
    db.commit()
PY
```

Then sign in, change the password flow via your IdP/SSO layer (the JWT layer is intentionally
small so it can be swapped for OIDC at the reverse proxy or with a custom dependency).

## Scaling

| Load | Shape |
|---|---|
| ≤5 cameras | 1 instance, SQLite, local disk |
| 5–40 cameras | 2–4 workers, Postgres, S3/R2 for evidence |
| 40+ cameras | Horizontal replicas behind a load balancer, Postgres, S3, Redis-backed rate limiting, move recognition to a queue worker (the engine is pure and trivially parallelisable) |

Because recognition is CPU-bound and in-process, scale with **replicas × workers**, not threads.
Each worker keeps its own glyph bank in memory (~30 MB).

## Backups

* Database: `pg_dump` nightly (captures reference storage keys, so restore both).
* Evidence: versioned bucket or `rsync` of `STORAGE_DIR`; keys are date-partitioned
  (`<org>/YYYY/MM/DD/…`) so retention can be applied with a lifecycle rule.
* Glyph bank cache is disposable — it rebuilds on boot.
