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
| **Camera console** | An iOS-style camera app that opens straight into a live viewfinder: shutter, scene-gated auto-scan, a Photos-style history grid, per-character confidence, correction-driven training, and server-rendered PDF evidence reports. |
| **Multi-tenant & RBAC** | Organisation-scoped data, four roles (`viewer → operator → supervisor → admin`), JWT sessions or scoped API keys. |
| **Audit trail** | Every login, capture, review, correction, export, key and webhook change is appended to an immutable log with request id and IP. |
| **Signed webhooks** | `capture.created` and `watchlist.hit` delivered with HMAC-SHA256 signatures and retry/backoff. |
| **Production ready** | Non-root Docker image, compose stack with Postgres, Kubernetes/Fly/Render/Railway manifests, init Job, health probes, Prometheus metrics, JSON logs, fail-fast config validation, CI with an accuracy gate. |

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
cp .env.example .env        # set SECRET_KEY and POSTGRES_PASSWORD
docker compose up --build
# → http://localhost:8000  (app + Postgres + persistent volumes)
```

Every platform above runs the image's entrypoint, so the container validates its configuration,
prepares the schema once (`scripts/init_db.py`) and then starts the workers. In production it refuses to boot on a weak `SECRET_KEY`, on
`DEBUG=true`, or on an unreachable S3 bucket instead of quietly falling back to local disk.

### Deploy to a cloud

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Omprakash-prog03/python-project-deploy)

The Render blueprint provisions the service, a 5 GB disk, a generated `SECRET_KEY` and
`COOKIE_SECURE=true`. The other targets are one command each:

| Target | Command |
|---|---|
| Kubernetes | `kubectl apply -f k8s/platevision.yaml` (secret + init Job included) |
| Fly.io | `fly launch --no-deploy && fly secrets set SECRET_KEY=$(openssl rand -hex 32) && fly deploy` |
| Render | New Blueprint → point at this repo (`render.yaml`) |
| Railway | New project → Deploy from repo (`railway.json`, Dockerfile build) |
| Any VM | `docker build -t platevision . && docker run -p 8000:8000 --env-file .env platevision` |
| Bare metal | `python scripts/init_db.py && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2 --proxy-headers` |

Postgres is supported out of the box (`psycopg[binary]` is a pinned dependency, driver URL scheme
`postgresql+psycopg://`); object storage is an optional extra (`requirements-s3.txt`, or build
with `--build-arg INSTALL_S3=1`). See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the full
matrix of deployment targets, hardening, backups, scaling and the production checklist.

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

Two harnesses, because they answer different questions.

**`scripts/bench_alpr.py` — the real measurement.** 1000 frames across four plate grammars
(IN / US / UK / EU) pushed through a fourteen-condition degradation matrix: tilt to 24°,
motion blur, distance, sensor noise, night, harsh JPEG, dirt, downscale and a combined
"hard mix". It reports exact match, per-character accuracy, false positives on empty frames
and latency percentiles, broken down per condition.

```
python scripts/bench_alpr.py --rounds 1000
```

| | Result |
|---|---|
| Exact match | **61.1%** |
| Character accuracy | **81.1%** |
| False positives (83 empty frames) | **1 (1.2%)** |
| Latency p50 / p95 | **163 ms / 454 ms** |

| Condition | Exact | | Condition | Exact |
|---|---|---|---|---|
| clean | 94.4% | | night | 54.9% |
| distant | 91.5% | | motion blur | 55.6% |
| low JPEG | 85.9% | | dirty plate | 57.8% |
| sensor noise | 83.1% | | downscaled | 70.4% |
| tilt 16° | 75.0% | | tilt 24° | 22.2% |
| heavy blur | 40.3% | | hard mix | 29.6% |

**Read that honestly.** A close, clean, well-lit plate is read essentially every time. A
distant, tilted, blurred night frame is not: this is a classical template-and-grammar pipeline
with no trained network, and 61% exact match over a deliberately harsh matrix is what that
architecture delivers. It is not commercial ANPR accuracy and the table above is the evidence,
not a marketing claim. The corpus is also synthetic — rendered from the same font family the
glyph bank uses — so these are an *upper bound* on field performance.

What the number is good for is comparison. Every change to the reader is now judged against it.

**`scripts/eval_alpr.py` — the smoke test.** 15 plates from the shipped pool, clean and
camera-like. It is fast enough for CI and catches gross breakage, but it over-reports: the
plates overlap the glyph bank's own tuning data.

| Corpus | Exact match | Character accuracy |
|---|---|---|
| Clean frames | **15/15 (100%)** | 100.0% |
| Degraded frames | **14/15 (93.3%)** | 93.3% |

Defects the benchmark found and this release fixes:

* **Tilted plates were rejected by the detector's own aspect gate.** The candidate box is the
  axis-aligned bound of a rotated rect, so a 3.2:1 plate measured 1.7:1 and failed the filter.
  Aspect now comes from the quad, and a rotation sweep covers angles past ~12°, where a
  horizontal closing kernel smears the plate into one blob.
* **The rotation sweep mapped boxes through the wrong coordinate space**, leaving every swept
  candidate offset by the downscale factor.
* **Two or three rectifications were computed per candidate and all but one discarded.** The
  variants fail independently, so the engine now votes across them — and a truncated read that
  merely collected more votes cannot win, because a clipped character is systematic.

Across a 400-frame A/B: character accuracy 79.9% → 82.9%, exact match 60.5% → 61.8%,
tilt-24 17% → 38%. The cost is latency, 83 ms → 160 ms p50, paid only on frames that need the
extra hypotheses.

`tests/test_alpr.py` gates both: an accuracy floor and a tilted-plate detection check, set well
below measured values so they fail on regressions rather than noise.

### Corrections train the reader

Every verified correction is training data. `POST /api/v1/captures/{id}/teach` re-reads the
stored frame, aligns the glyphs it produced against the corrected spelling (global alignment,
substitutions only — a length change means segmentation failed, so nothing is learned), and
stores those crops as exemplars for the correct class in
`MODEL_CACHE_DIR/learned-glyphs-v1.npz`. The classifier scores them separately from the
rendered bank and blends them at 0.65 weight, so one correction is not diluted by 90 synthetic
templates. `GET /api/v1/learning` shows what has been learned; `DELETE /api/v1/learning`
forgets it. The console exposes the same flow as **Correct plate · trains this camera** in the
capture drawer.
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
| `POST` | `/api/v1/captures/{id}/teach` | Correct the reading *and* train the glyph classifier |
| `GET` | `/api/v1/captures/export.pdf` | Every stored scan as a paginated PDF |
| `GET` | `/api/v1/learning` | Learned exemplar stats · `DELETE` resets them (admin) |
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

The console (`app/static/`) is a camera, not a dashboard. It opens straight into a full-bleed
viewfinder with an automatic demo session; everything else arrives as a sheet over it.
Dependency-free ES2020 — no build step, no CDN — so it works in air-gapped deployments.

Two materials, kept apart on purpose:

* **The viewfinder is a camera.** Black in both themes, glass chrome, gold reserved for live
  state. A white camera matrix is not a camera.
* **Every other surface is paper.** White, generous margins, one hero object, hairlines instead
  of fills, and type carrying the hierarchy. Follows the system appearance.

* **Signature wordmark** — vendored Sacramento (OFL), set alone at the top.
* **Ghost pill** — one hairline affordance, *Import a photo*, floating on the viewfinder.
* **Transport capsule** — one grey pill with three glyphs (photo / scan / import), no labels.
* **Shutter** — 74 px ring, thumbnail with a count on the left, flip on the right.
* **Filmstrip** — recent scans in one row, newest ringed with a paper gap, dates bookending
  the strip and a faint `‹‹ swipe history ››` caption.
* **Floating dock** — round menu, a pill holding the name and a refresh arrow, and an offset
  square for import.
* **Result sheet** — a card floating on white with a disc behind it, a grey kicker over a bold
  plate, the download as a hairline pill, and per-character confidence as chips.
* **History** — Photos-style three-column grid under sticky day headers, with
  All / Today / This week, and the whole history as one PDF.
* **Correct & train** — retype a plate and the misread glyphs become training data for that
  camera (see *Corrections train the reader*).

Auto-scan adapts its interval to the observed read time (`max(2.6 s, last read x 1.5)`) and
gates each tick on a 32x18 luma fingerprint, so an unchanged scene is skipped instead of being
re-read. It pauses while a sheet is open or the tab is hidden.

On a screen 700 px or wider the camera renders as a phone-shaped column, and sheets become
popovers sized to that column.

Cut deliberately: KPI tiles, charts, filters, sorting, watchlist UI, camera fleet and audit
screens. The API still exposes all of it — the console just does not need it to scan a plate.

### PDF reports

`app/services/pdf.py` writes PDF 1.4 directly — no ReportLab, no wkhtmltopdf, no headless
browser. It owns the object table, cross-reference offsets and page tree; images are embedded
as `DCTDecode` XObjects and the two text faces ship as 20 kB TrueType subsets in
`app/assets/pdf/`.

Embedding the fonts is not decoration. The first version used base-14 Helvetica with widths
from the standard AFM table, and every viewer substituted a different face — Chrome, Preview
and Acrobat disagreed by up to 19 % on the same string, so right-aligned columns drifted and
overlapped. Measuring from the same bytes the PDF carries makes the layout identical
everywhere.

| Report | Endpoint | Contents |
|---|---|---|
| Evidence sheet | `GET /api/v1/captures/{id}/report.pdf` | Plate, full frame, confidence, glyphs, provenance |
| Scan history | `GET /api/v1/captures/export.pdf` | Every stored scan, 7 per page |

The layout is mobile-first: a single column capped at 440 px, with the viewfinder filling the
screen, a control tray beneath it and a dock (history · shutter · PDF) at the bottom.

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
| `ALLOWED_HOSTS` | `*` | Comma-separated hosts accepted by `TrustedHostMiddleware` |
| `COOKIE_SECURE` | auto in production | Session cookies require HTTPS unless explicitly disabled |
| `BOOTSTRAP_ON_START` | `true` | Set `false` in containers — the entrypoint/init Job prepares the schema |
| `SEED_DEMO_DATA` | `true` | Set `false` for a clean tenant |
| `ALLOW_DEMO_LOGIN` | `true` | Disable the demo shortcut in production |

`GET /api/v1/config` returns the public, secret-free subset (name, version, environment, demo
login availability, countries, upload limit) so the console can render itself correctly without
baked-in credentials.

---

## Development

```bash
make install     # runtime + dev dependencies
make dev         # autoreload server
make test        # 70 tests: engine, grammar, API, security, metrics, deployment
make lint        # ruff
make eval        # accuracy harness

python scripts/smoke.py --base http://localhost:8000   # 14-step live check
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
tests/ | scripts/ | docker/ | k8s/ | docs/
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
* Production configuration is validated at startup: weak secrets, `DEBUG`, an unreachable
  evidence bucket and wildcard-with-credentials CORS are rejected, not warned about.
* Data residency: all inference is local, so frames never leave your network.

## Licence

MIT for the application code. Bundled typefaces are OFL (Cormorant Garamond, Inter, Archivo,
Barlow Condensed, Oswald, Roboto Condensed) and the DejaVu licence — see
`app/static/fonts/` and `app/assets/fonts/`.
