# PlateVision

**Commercial licence-plate intelligence — recognition, watchlist enforcement, analytics and audit.**

PlateVision turns any camera frame into an operational record: a plate string, a confidence
score, a jurisdiction-validated format, a risk tier, and an audit trail an auditor will accept.
Everything runs on your own infrastructure — no third-party OCR API, no per-scan billing,
no frames leaving your network.

```
┌─────────────────────────────┐      ┌──────────────────────────────┐
│  Camera / upload / API      │ ───► │  ALPR engine (in-process)     │
│  jpg · png · webp · stream  │      │  detect → rectify → segment   │
└─────────────────────────────┘      │  → classify → grammar decode  │
                                     └──────────────┬───────────────┘
                                                    ▼
      ┌───────────────┬────────────────┬──────────────────────┐
      │ Evidence store│ Watchlist      │ Analytics + audit    │
      │ (disk/S3)     │ enforcement    │ KPIs, trends, webhooks│
      └───────────────┴────────────────┴──────────────────────┘
```

---

## Highlights

| Capability | Detail |
|---|---|
| **Self-hosted ALPR** | Detection, perspective rectification, character segmentation, hybrid template/k-NN classification and grammar-constrained decoding. No GPU, no model download, no network calls at inference. |
| **Jurisdiction grammars** | India (HSRP, standard, commercial, BH-series, defence), US, UK, EU and generic fallbacks. Ambiguous glyphs (`O/0`, `I/1`, `B/8`, `S/5`, `Z/2`) are resolved with an edit-distance dynamic program, not by naive argmax. |
| **Watchlist enforcement** | Stolen / banned / permit-lapsed / VIP / unpaid categories with severity, near-miss (1-character) matching, and live alerting. |
| **Operator console** | Dark, editorial single-page app: live KPIs, volume chart, review queue, capture drawer with per-character confidence, watchlist management, camera fleet, insights. |
| **Multi-tenant & RBAC** | Organisation-scoped data, four roles (`viewer → operator → supervisor → admin`), JWT sessions or scoped API keys. |
| **Audit trail** | Every login, capture, review, correction, export, key and webhook change is appended to an immutable log with request id and IP. |
| **Signed webhooks** | `capture.created` and `watchlist.hit` delivered with HMAC-SHA256 signatures and retry/backoff. |
| **Production ready** | Docker image, compose stack with Postgres, Fly/Render/Railway manifests, health probes, Prometheus metrics, structured logs, rate limiting, CI with an accuracy gate. |

---

## Quick start

```bash
# 1. dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 2. run (SQLite + demo workspace, seeded on first boot)
uvicorn app.main:app --reload --port 8000
```

* Console → <http://localhost:8000>
* API docs → <http://localhost:8000/docs>
* Health → <http://localhost:8000/api/v1/health>

**Demo credentials** — `elara@platevision.io` / `nightwatch`

The first boot provisions *Meridian Traffic Authority*: 5 users, 6 cameras, 6 watchlist
entries and 34 captures produced by running the real engine over synthetic night frames,
so the dashboard shows genuine detections, confidences and timings.

### Docker (one command)

```bash
docker compose up --build
# → http://localhost:8000  (app + Postgres + persistent volumes)
```

### Deploy to a cloud

| Target | Command |
|---|---|
| Fly.io | `fly launch --no-deploy && fly secrets set SECRET_KEY=$(openssl rand -hex 32) && fly deploy` |
| Render | New Blueprint → point at this repo (`render.yaml`) |
| Railway | New project → Deploy from repo (`railway.json`, Dockerfile build) |
| Any VM | `docker build -t platevision . && docker run -p 8000:8000 --env-file .env platevision` |
| Bare metal | `pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2 --proxy-headers` |

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for hardening, storage options and scaling.

---

## The recognition pipeline

```
frame ──► decode & orient (Pillow, EXIF)          ──► downsample to ≤1800 px
      ──► CLAHE contrast equalisation
      ──► candidate proposal:  morph ▹ MSER ▹ contours ▹ flipped pass
      ──► fusion: padded + union hypotheses, IoU de-duplication
      ──► plate-likeness verifier (character-height consistency)
      ──► rectification (homography / bilinear) at 96 px and 128 px
      ──► ink-mask polarity selection (dark-on-light vs light-on-dark)
      ──► text-line clustering → character bounding boxes
      ──► per-glyph classification: template correlation + HOG k-NN + structure
      ──► grammar-constrained decode (edit-distance DP over format patterns)
      ──► jurisdiction metadata, risk scoring, persistence, webhooks
```

Design notes worth knowing:

* **Why template matching instead of a CNN?** It runs on a 1 vCPU toll-plaza box with no GPU,
  no 100 MB weights to ship, and no cold-start penalty — and it is fully explainable, which
  matters when a plate read becomes evidence in a dispute. The bank is rendered from six
  typefaces at boot and cached (`storage/models/glyphbank-*.npz`), so restarts are instant.
* **Why grammar decoding?** Character classifiers are strongest at ranking, weakest at
  absolute certainty. A plate is not free text: `SS RR L NNNN` in India, `LL NN LLL` in the UK.
  An edit-distance DP that jointly aligns glyph probabilities to every candidate pattern
  recovers strings the raw argmax gets wrong — and it rejects impossible reads.
* **Multiple hypotheses.** A detector box that clips one character destroys the read, so the
  reader tries padded and merged boxes plus two rectification resolutions and keeps the
  highest-confidence, format-valid interpretation.

### Measured accuracy

Sample corpus: 15 plates rendered with the shipped fonts, in both a *clean* setting and a
*camera-like* setting (rotation ±6.5°, perspective squash, motion blur, sensor noise, JPEG).

| Corpus | Exact match | Character accuracy | Warm latency (avg / p95) |
|---|---|---|---|
| Clean frames | **13/15 (86.7%)** | 90.0% | 114 ms / 159 ms |
| Degraded frames | **11/15 (73.3%)** | 77.3% | 231 ms / 377 ms |

Reproduce with `make eval` / `make eval-deg` (or `python scripts/eval_alpr.py --degraded`).
Every read also reports a confidence; low-confidence reads are routed to the review queue
rather than silently trusted. For production deployments, add your own jurisdiction corpus to
`scripts/eval_alpr.py` and calibrate `ALPR_REVIEW_CONFIDENCE` before enabling automation.

---

## API

OpenAPI is generated automatically at `/docs` (Swagger) and `/redoc`. All responses are
JSON; errors use a single envelope:

```json
{ "error": { "code": "not_found", "message": "Capture not found.", "request_id": "1f6…", "details": {} } }
```

### Recognise a plate (no persistence)

```bash
curl -X POST http://localhost:8000/api/v1/recognize \
  -H "Authorization: Bearer $TOKEN" \
  -F file=@frame.jpg -F country=IN
```

```json
{
  "plates": [{
    "plate": "MH12AB3456",
    "confidence": 0.86,
    "format": "IN_NEW",
    "format_valid": true,
    "box": {"x": 304, "y": 202, "w": 410, "h": 137},
    "region": "MH",
    "diagnostics": {"region_name": "Pune, Maharashtra", "glyph_count": 10, "quality": 0.72},
    "glyphs": [{"label": "M", "confidence": 0.97, "box": {...}}, "…"],
    "alternates": [{"plate": "MH12AB34S6", "confidence": 0.41}]
  }],
  "processing_ms": 96.4,
  "engine_version": "pv-alpr/1.2"
}
```

### Store evidence with watchlist enforcement

```bash
curl -X POST http://localhost:8000/api/v1/captures \
  -H "Authorization: Bearer $TOKEN" \
  -F file=@frame.jpg -F camera_id=$CAMERA_ID -F notes="Gate 2 inbound"
```

The response carries `watchlist_hit`, `risk_level`, `status` (`verified` / `pending` /
`flagged`) and stable image URLs.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/auth/login` · `/auth/demo` · `/auth/refresh` · `/auth/logout` | Sessions |
| `GET` | `/api/v1/auth/me` | Current user + quota |
| `POST` | `/api/v1/captures` | Upload + recognise + persist |
| `POST` | `/api/v1/captures/batch` | Up to 10 frames |
| `GET` | `/api/v1/captures` | Filter by plate fragment, status, risk, camera, date; sort; paginate |
| `GET` | `/api/v1/captures/{id}/image` | Evidence image (`?variant=thumb`) |
| `PATCH` | `/api/v1/captures/{id}` | Verify / flag / deny, correct plate, add note |
| `POST` | `/api/v1/captures/{id}/reanalyze` | Re-run inference on stored original |
| `GET` | `/api/v1/captures/export.csv` | Audited CSV export (≤10 000 rows) |
| `POST` | `/api/v1/recognize` · `/recognize/annotated` | Stateless read / annotated JPEG |
| `GET` | `/api/v1/recognize/samples` | Synthetic frames for demos & CI |
| `GET/POST/PATCH/DELETE` | `/api/v1/watchlist…` | List, add, toggle, remove, matches, stats |
| `GET` | `/api/v1/analytics/overview` · `/heatmap` | KPIs, series, breakdowns |
| `GET/POST` | `/api/v1/cameras`, `/cameras/health` | Fleet registry |
| `GET` | `/api/v1/health` · `/metrics` · `/audit` · `/info` | Operations |
| `GET/POST/DELETE` | `/api/v1/api-keys`, `/webhooks` | Integrations |

### API keys

```bash
curl -X POST /api/v1/api-keys -H "Authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' -d '{"name":"Toll plaza edge","scopes":["captures:read"]}'
# → {"token":"pv_…"}  (shown once; store it in your secret manager)

curl /api/v1/captures -H "X-API-Key: pv_…"
```

### Webhooks

Every capture fans out to subscribed endpoints when `capture.created` / `watchlist.hit`
fires. Verify the signature before trusting the payload:

```python
import hashlib, hmac
expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, request.headers["X-PlateVision-Signature"])
```

---

## Console

The single-page console (`app/static/`) is dependency-free ES2020 — no build step, no CDN,
so it works in air-gapped deployments.

* **Overview** — greeting header, cinematic hero, live corridor badge, KPI tiles, volume chart
  (24h / 7d / 30d), watchlist rail, recent activity, high-risk frames.
* **Scan** — drag-and-drop or camera samples; annotated overlay, per-character confidence
  chips, alternates, latency and quality readouts.
* **Library** — filters (needs review, verified, flagged, critical risk), sorting, pagination,
  audited CSV export, capture drawer with circular review controls.
* **Watchlist** — visual cards, category mix, recent matches, add-entry modal.
* **Insights** — plate-type / risk / status breakdowns and camera fleet health.

The layout is mobile-first: below 860 px the rail becomes a floating glass tab bar, exactly
like the reference design language (near-black canvas, dusk gradients, Cormorant display
serif, Inter utility type, lilac accent `#C9B8FF`).

---

## Configuration

All settings are environment variables (see `.env.example`); the ones that matter most:

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `development` | `production` enables JSON logs, HSTS and secret-length enforcement |
| `SECRET_KEY` | dev placeholder | **Required** in production (≥32 chars) |
| `DATABASE_URL` | `sqlite:///./storage/platevision.db` | Use Postgres for multi-instance deployments |
| `STORAGE_BACKEND` | `local` | `s3` for object storage (S3, R2, MinIO) |
| `STORAGE_DIR` / `MODEL_CACHE_DIR` | `./storage/…` | Must be writable; mount a volume |
| `ALPR_DEFAULT_COUNTRY` | `IN` | Drives grammar priors |
| `ALPR_REVIEW_CONFIDENCE` | `0.72` | Reads below this land in the review queue |
| `MAX_UPLOAD_MB` | `12` | Hard limit enforced before inference |
| `RATE_LIMIT_PER_MINUTE` / `_BURST` | `240` / `60` | Token bucket per key/IP |
| `CORS_ORIGINS` | `*` | Restrict to your console origin in production |
| `SEED_DEMO_DATA` | `true` | Set `false` for a clean tenant |
| `ALLOW_DEMO_LOGIN` | `true` | Disable the demo shortcut in production |

---

## Development

```bash
make install     # runtime + dev dependencies
make dev         # autoreload server
make test        # 49 tests: engine, grammar, API, security, metrics
make lint        # ruff
make eval        # accuracy harness
```

```
app/
├── alpr/          # detection, imaging, OCR bank, grammar, engine
├── api/routes/    # auth · captures · recognize · watchlist · analytics · cameras · system
├── core/          # config, logging, security, errors, metrics, rate limiting
├── db/ models/    # SQLAlchemy 2.0 entities and sessions
├── schemas/       # Pydantic v2 request/response contracts
├── services/      # storage, recognition, analytics, annotate, webhooks, audit, samples
├── static/        # console (html · css · js · fonts · imagery)
├── assets/fonts/  # glyph bank sources
├── main.py        # application factory, middleware, error envelope
└── seed.py        # idempotent demo provisioning
tests/ | scripts/ | docker/ | docs/
```

More: [Architecture](docs/ARCHITECTURE.md) · [Deployment](docs/DEPLOYMENT.md) ·
[API cookbook](docs/API.md) · [Number-plate project brief (original)](docs/legacy/)

---

## Security & compliance notes

* Passwords: scrypt (N=2¹⁴, r=8, p=1) with per-user salts; constant-time verification.
* Sessions: short-lived HS256 access JWT in an `HttpOnly`, `SameSite=Lax` cookie (or bearer),
  refresh token scoped to `/api/v1/auth`. Set `COOKIE_SECURE=true` behind TLS.
* API keys are stored as scrypt hashes; the plaintext is shown once at creation.
* Every response carries a request id, and security headers (CSP, nosniff, referrer policy,
  HSTS in production). Mutable operations are rate-limited and written to the audit log.
* Evidence retention is your policy: `DELETE /captures/{id}` removes the row and both the
  original and thumbnail objects (supervisor+ only, audited).
* Data residency: all inference is local, so frames never leave your network.

## Licence

MIT for the application code. Bundled typefaces are OFL (Cormorant Garamond, Inter, Archivo,
Barlow Condensed, Oswald, Roboto Condensed) and the DejaVu licence — see
`app/static/fonts/` and `app/assets/fonts/`.
