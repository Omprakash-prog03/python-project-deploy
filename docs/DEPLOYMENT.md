# Deployment guide

PlateVision is one stateless web service with two stateful dependencies: a database and an
evidence store. Everything else — fonts, glyph bank sources, console, imagery — ships inside
the image, so a deploy is "run the container, point it at Postgres and a bucket".

Supported and exercised in this repository:

| Target | Artifact | Status |
|---|---|---|
| Docker / Compose (app + Postgres) | `Dockerfile`, `compose.yaml` | manifests validated; image build not run in the authoring sandbox (no Docker daemon) |
| Kubernetes | `k8s/platevision.yaml` | manifests validated + assertions in `tests/test_deployment.py` |
| Fly.io | `fly.toml` | validated |
| Render | `render.yaml` | validated |
| Railway | `railway.json` | validated |
| Bare VM + systemd/nginx | this document | validated |

The application itself was booted in production mode against **PostgreSQL 16** with **2 uvicorn
workers**: schema bootstrap, 80 concurrent requests, the 14-step smoke suite and the auth/cookie
path all pass (see [Verification](#verification)).

---

## 1. How the container starts

```
docker/entrypoint.sh
├── validate SECRET_KEY (production, ≥32 chars)          → exit 1 on failure
├── mkdir STORAGE_DIR / MODEL_CACHE_DIR
├── force WEB_CONCURRENCY=1 for SQLite
├── python scripts/init_db.py
│     ├── wait up to 45 s for the database (compose/k8s friendly)
│     ├── take a lock (Postgres advisory lock / SQLite flock)   ← replica-safe
│     ├── create missing tables
│     ├── seed the demo workspace when SEED_DEMO_DATA=true (idempotent)
│     └── warm the glyph bank (~3 s, cached afterwards)
└── exec uvicorn --workers ${WEB_CONCURRENCY:-2} --proxy-headers
```

Two knobs follow from this:

* **`BOOTSTRAP_ON_START`** (default `true`) — the *application* also prepares the schema in its
  lifespan. Containers set it to `false` because the entrypoint already did the work before the
  workers forked. Leave it `true` only if you run `uvicorn app.main:app` directly (bare metal).
  Running both is harmless (the work is idempotent) but duplicates the wait.
* **`docker run … init | seed | shell`** — the entrypoint accepts a subcommand for one-off jobs:
  `docker compose run --rm api init`, `… seed`, `… shell`.

Because `exec` replaces the shell, the process is PID 1 and receives `SIGTERM` directly: the
preStop/termination grace period drains connections properly.

---

## 2. Configuration

Everything is environment-driven (see `.env.example`). Production behaviour is enforced in
`app/core/config.py` — the process **refuses to start** rather than degrade silently.

### Hard failures in production

| Condition | Result |
|---|---|
| `SECRET_KEY` missing, shorter than 32 chars, or the shipped dev placeholder | `RuntimeError` at import time |
| `DEBUG=true` | `RuntimeError` |
| `STORAGE_BACKEND=s3` without `S3_BUCKET` | `RuntimeError` |
| `STORAGE_BACKEND=s3` with an unreachable bucket (missing boto3, bad credentials, no bucket) | `RuntimeError` at first use — **no silent fallback to local disk** |
| Entrypoint exits non-zero | container restarts; Kubernetes keeps the old ReplicaSet |

### Automatic and advisory behaviour

| Setting | Production default | Note |
|---|---|---|
| `COOKIE_SECURE` | forced `true` unless set explicitly | set `false` only for a plain-HTTP internal deployment |
| `ALLOW_DEMO_LOGIN=true` | warning | anyone can sign in with the demo account |
| `SEED_DEMO_DATA=true` | warning | creates *Meridian Traffic Authority* on boot |
| `CORS_ORIGINS=*` | warning | credentialed CORS is automatically disabled for `*` (browsers reject the combination) |
| `ALLOWED_HOSTS=*` | warning | otherwise enforced by `TrustedHostMiddleware` (a request for an unknown Host gets `400`) |
| SQLite in production | warning | single instance only |

Advisories are logged at boot and exposed (as non-secret names) at `GET /api/v1/config`, which
the console uses to decide whether to show the demo sign-in button.

### Reference

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `development` | `production` → JSON logs, HSTS, config guards |
| `SECRET_KEY` | dev placeholder | required in production |
| `DATABASE_URL` | `sqlite:///./storage/platevision.db` | `postgresql+psycopg://user:pass@host:5432/db` for Postgres |
| `STORAGE_BACKEND` | `local` | `local` \| `s3` |
| `STORAGE_DIR` / `MODEL_CACHE_DIR` | `./storage/…` | must be writable; volume or `emptyDir` |
| `S3_BUCKET` / `S3_REGION` / `S3_ENDPOINT_URL` | – / `ap-south-1` / – | S3, R2, MinIO, B2 |
| `BOOTSTRAP_ON_START` | `true` | `false` in containers |
| `WEB_CONCURRENCY` | `2` | forced to `1` for SQLite |
| `ALLOWED_HOSTS` | `*` | comma-separated hosts accepted by `TrustedHostMiddleware` |
| `CORS_ORIGINS` | `*` | comma-separated origins |
| `COOKIE_SECURE` / `COOKIE_DOMAIN` | `false` / – | auto-`true` in production |
| `ALLOW_DEMO_LOGIN` / `DEMO_EMAIL` / `DEMO_PASSWORD` | `true` / demo account | disable for real tenants |
| `SEED_DEMO_DATA` | `true` | set `false` for a clean install |
| `ALPR_*` | see `.env.example` | confidence thresholds, countries, limits |
| `MAX_UPLOAD_MB` | `12` | enforced before inference |
| `RATE_LIMIT_PER_MINUTE` / `_BURST` | `240` / `60` | in-process token bucket, per worker (see §10) |
| `WEBHOOK_TIMEOUT_SECONDS` / `_MAX_ATTEMPTS` | `5` / `3` | signed delivery retries |

---

## 3. Docker

```bash
docker build -t platevision/api:1.0.0 .                    # add --build-arg INSTALL_S3=1 for S3
docker run --rm -p 8000:8000 \
  -e ENVIRONMENT=production \
  -e SECRET_KEY=$(openssl rand -hex 32) \
  -e DATABASE_URL=sqlite:////data/platevision.db \
  -e STORAGE_DIR=/data/uploads \
  -e MODEL_CACHE_DIR=/data/models \
  -v platevision:/data \
  platevision/api:1.0.0
```

The image runs as uid/gid 1001 (`platevision`), has a `HEALTHCHECK` on `/healthz`, keeps only
`libgl1`/`libglib2.0-0` at runtime, and ships `psycopg[binary]` so Postgres works out of the box.
`boto3` is deliberately **not** in the base image (≈30 MB); opt in with `INSTALL_S3=1` or
`requirements-s3.txt`.

## 4. Compose (app + Postgres)

```bash
cp .env.example .env        # set SECRET_KEY and POSTGRES_PASSWORD
docker compose up -d --build
docker compose exec api python scripts/init_db.py --seed    # only if SEED_DEMO_DATA=false
```

Volumes: `captures` (evidence), `models` (glyph cache), `pgdata`. The API waits for the database
healthcheck, runs `init_db.py`, then starts two workers.

## 5. Kubernetes

```bash
kubectl create namespace platevision
kubectl -n platevision create secret generic platevision-secrets \
  --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
  --from-literal=DATABASE_URL="postgresql+psycopg://user:pass@db-host:5432/platevision"
aws s3 mb s3://platevision-evidence --region ap-south-1   # evidence bucket
docker build --build-arg INSTALL_S3=1 -t ghcr.io/example/platevision:1.0.0 .
kubectl apply -f k8s/platevision.yaml
```

The manifest ships: namespace, service account (IRSA annotation example), ConfigMap (non-secret
config only), an init **Job** that runs `init_db.py` (`argocd.argoproj.io/hook: PreSync`),
a 2-replica Deployment with startup/readiness(`/api/v1/health`)/liveness(`/healthz`) probes and a
`preStop` drain, Service, Ingress (TLS, 16 MB body limit), PodDisruptionBudget and an HPA
(2–8 replicas @ 70 % CPU).

Notes:

* `DATABASE_URL` and `SECRET_KEY` live in the Secret; Kubernetes does **not** expand `$(VAR)`
  inside a ConfigMap, and a test asserts the ConfigMap never contains placeholders.
* Default storage is S3 because replicas cannot share local disk. For a single-node install:
  `STORAGE_BACKEND=local`, `STORAGE_DIR=/data/uploads`, a `ReadWriteOnce` PVC at `/data`, and
  `replicas: 1`.
* Init Job runs on every release. With plain `kubectl` (no Argo) use
  `kubectl delete job platevision-init --ignore-not-found` before applying, or rely on
  `ttlSecondsAfterFinished` and the idempotent, advisory-locked script.

## 6. Fly.io

```bash
fly launch --no-deploy                              # keeps fly.toml
fly volumes create platevision_data --size 5
fly secrets set SECRET_KEY=$(openssl rand -hex 32)
fly deploy
```

`fly.toml` pins `bom` (Mumbai) and defaults to SQLite on a volume with **one worker** — SQLite
cannot take concurrent writers. Raise `WEB_CONCURRENCY` only after moving to Postgres
(`fly postgres attach`).

## 7. Render / Railway

* **Render** — create a Blueprint from the repo; `render.yaml` provisions the Docker service,
  a 5 GB disk at `/var/data`, a generated `SECRET_KEY`, `COOKIE_SECURE=true`, and one worker
  (SQLite). Add `DATABASE_URL` to move to Postgres and raise `WEB_CONCURRENCY`.
* **Railway** — `railway.json` selects the Dockerfile builder, `/healthz` health check and
  restart policy. Mount a volume at `/data` and set `DATABASE_URL`, `STORAGE_DIR`,
  `MODEL_CACHE_DIR`, `SECRET_KEY`, `SEED_DEMO_DATA=false`. The start command defaults to one
  worker; override `WEB_CONCURRENCY` after attaching Postgres.

## 8. Bare metal (systemd + nginx)

```ini
# /etc/systemd/system/platevision.service
[Unit]
Description=PlateVision
After=network.target

[Service]
User=platevision
WorkingDirectory=/opt/platevision
EnvironmentFile=/etc/platevision.env
ExecStartPre=/opt/platevision/.venv/bin/python scripts/init_db.py
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

With `--proxy-headers`, rate limiting keys on the real client IP from `X-Forwarded-For`; make
sure only the reverse proxy can reach port 8000.

---

## 9. Database

### Postgres (recommended for anything beyond a demo)

```
DATABASE_URL=postgresql+psycopg://platevision:secret@db:5432/platevision
```

* Driver: `psycopg[binary]==3.2.3`, pinned in `requirements.txt`. No system packages needed.
* Pool: queue pool, 10 connections + 20 overflow per worker, `pool_pre_ping=True`,
  `pool_recycle=1800` — safe for managed Postgres with idle timeouts.
* Sizing: `workers × 30` is the ceiling of open connections per replica; keep
  `max_connections` of the database above `replicas × workers × 10` in normal operation.
* Schema: `scripts/init_db.py` waits for the server, takes `pg_advisory_lock(0x50565F31)`,
  creates missing tables and seeds if requested. Four concurrent runs against PostgreSQL 16
  produced exactly one tenant and no errors.

### SQLite (single instance only)

WAL mode, `busy_timeout=5000`, `foreign_keys=ON`, 30 s connect timeout. The entrypoint forces one
worker; multiple replicas sharing one file are unsupported (use Postgres).

### Schema evolution

v1.0 owns its schema through `Base.metadata.create_all` — it creates **missing tables** but does
not alter existing ones. The schema is therefore frozen for 1.0. Before the first change to an
existing table, add a migration tool (Alembic) and baseline it with `alembic stamp head` on
existing databases; until then, any change must ship as an explicit, reviewed `ALTER TABLE`
executed by the same init Job (`psql -f` or a one-off Python script) **before** the rollout that
expects it. Do not edit tables by hand in production without a backup.

---

## 10. Storage, health, observability

**Evidence store**

* `local` — files under `STORAGE_DIR` with date-partitioned keys (`<org>/YYYY/MM/DD/…`).
  Requires a volume; per-replica disk means a capture written by pod A 404s on pod B.
* `s3` — any S3-compatible endpoint (`S3_ENDPOINT_URL` for MinIO/R2). The bucket is checked with
  `head_bucket` at startup; failure is fatal in production. Credentials come from the standard
  AWS chain (env vars, IRSA, instance profile).

**Probes**

| Path | Purpose | Behaviour |
|---|---|---|
| `/healthz` | liveness | process answer only; never touches the database |
| `/api/v1/health` | readiness | database ping, storage backend, engine status, uptime |
| `/api/v1/metrics` | Prometheus | request counters/latency, captures, queue depth, health |

**Rate limiting** is an in-process token bucket keyed by API key or client IP, **per worker**:
two workers and two replicas allow roughly four times the configured budget. That is acceptable
for abuse protection; if you need exact global quotas, put enforcement in the ingress
(`limit-rps`) or introduce a Redis-backed limiter.

**Logs** are JSON in production, one line per request, carrying `request_id`, user id, method,
path, status and duration. Ship stdout/stderr — the container writes nothing else.

**Alerts worth having**: `platevision_health_ok == 0`, 5xx rate, `capture.created` silence per
site (a dead corridor looks exactly like a quiet one) and `storage` health != ok.

---

## 11. First admin on a clean deployment

`SEED_DEMO_DATA=false` gives you an empty install. Create the first tenant and admin once:

```bash
docker compose run --rm api python - <<'PY'
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

Sign in, then change the password through your IdP or the admin API. The JWT layer is deliberately
small so it can be swapped for OIDC at the reverse proxy (e.g. oauth2-proxy) or with a custom
dependency.

---

## 12. Production checklist

- [ ] `SECRET_KEY` from a secret manager (≥32 chars) — the app refuses to boot otherwise
- [ ] `ENVIRONMENT=production` → JSON logs, HSTS, config guards
- [ ] TLS in front; `COOKIE_SECURE=true` (automatic unless overridden)
- [ ] `ALLOWED_HOSTS` set to your domains (unknown Host → 400)
- [ ] `CORS_ORIGINS` restricted to the console origin
- [ ] `ALLOW_DEMO_LOGIN=false` and `SEED_DEMO_DATA=false` for a real tenant
- [ ] `DATABASE_URL` on Postgres with automated backups + PITR
- [ ] `STORAGE_BACKEND=s3` for anything with more than one replica
- [ ] Init Job (or entrypoint) runs `scripts/init_db.py` before the rollout
- [ ] Liveness `/healthz`, readiness `/api/v1/health` wired into the load balancer
- [ ] `/api/v1/metrics` scraped; alerts on health, 5xx and capture silence
- [ ] Log shipping in place; retention matches your evidence policy
- [ ] Evidence retention policy exercised (`DELETE /api/v1/captures/{id}` or bucket lifecycle)
- [ ] `ALPR_REVIEW_CONFIDENCE` calibrated against your own corpus before auto-clearance
- [ ] Database and evidence restore tested at least once

---

## 13. Scaling

Because recognition is CPU-bound and in-process, scale with **replicas × workers**, not threads.
Each worker keeps its own glyph bank in memory (~30 MB) and builds it once (~3 s, cached on the
volume).

| Load | Shape |
|---|---|
| ≤5 cameras | 1 instance, SQLite, local disk |
| 5–40 cameras | 2–4 workers, Postgres, S3/R2 evidence |
| 40+ cameras | 2–8 replicas behind a load balancer, Postgres, S3, Redis/infra rate limiting, move inference to a queue worker |

---

## 14. Backups and restore

* **Database** — `pg_dump --format=custom` nightly + PITR if the provider offers it. Captures
  reference storage keys, so the database and the evidence store must be restored together.
* **Evidence** — versioned bucket or `rsync` of `STORAGE_DIR`. Keys are date-partitioned, so
  lifecycle rules can implement retention without touching the database.
* **Glyph cache** — disposable; rebuilds on boot.
* **Restore drill** — restore the dump into a scratch database, point a container at it with the
  same bucket, run `python scripts/smoke.py --base http://localhost:8000`; all 14 checks must pass
  before you call the backup good.

---

## 15. Verification

```bash
python -m pytest -q                 # 70 tests incl. config guards, manifests, container boot
python -m ruff check app tests scripts
python scripts/smoke.py --base http://localhost:8000      # 14 live checks, exit code 1 on failure
python scripts/eval_alpr.py         # accuracy gate (CI fails below the configured floor)
```

`scripts/smoke.py` is the deploy gate: liveness, readiness, login, recognition, camera fleet,
ingest, watchlist enforcement, review, evidence image, analytics, watchlist, CSV export, console
and metrics. Run it against every environment after a rollout.

---

## 16. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Container exits: `SECRET_KEY must be set…` | production with a missing/short/dev secret — generate `openssl rand -hex 32` |
| `400 Invalid host header` from every request | `ALLOWED_HOSTS` does not include the domain (or the load balancer forwards the internal host) |
| Pods crash: `S3 … unreachable` | bucket missing, or image built without `INSTALL_S3=1`, or no credentials/IRSA |
| `database is locked` | more than one worker/replica on SQLite — use Postgres |
| `psycopg.OperationalError: connection failed` | wrong URL scheme (use `postgresql+psycopg://`), unreachable host, missing password |
| `429 rate_limited` under load | token bucket exhausted; raise `RATE_LIMIT_*` or scale out |
| First request after deploy is slow | glyph bank not warmed — keep `MODEL_CACHE_DIR` on a persistent volume (init job warms it) |
| Login succeeds but the browser is not authenticated | `COOKIE_SECURE=true` over plain HTTP, or a cookie domain mismatch |
