# Architecture

## Runtime shape

```
                 ┌──────────────────────────────────────────────┐
  browser ──────►│ FastAPI + Starlette middleware               │
                 │  request id · metrics · security headers     │
                 │  gzip · CORS · error envelope                │
                 └───────┬──────────────────────────┬───────────┘
                         │                          │
              ┌──────────▼──────────┐    ┌──────────▼───────────┐
              │ API routers (v1)    │    │ Static console       │
              │ auth captures       │    │ /static + index.html │
              │ recognize watchlist │    └──────────────────────┘
              │ analytics cameras   │
              │ system              │
              └──────┬──────────────┘
                     │ services (business rules)
     ┌───────────────┼────────────────┬──────────────────┬───────────┐
     ▼               ▼                ▼                  ▼           ▼
 recognition     analytics        storage           webhooks      audit
 (ALPR engine)   (SQL aggregates) (disk | S3)       (HMAC+retry)  (append-only)
     │
     ▼
 app/alpr: imaging → detect → ocr → grammar → annotate
```

Everything is synchronous inside the request except outbound webhook delivery, which is
best-effort and never fails ingestion. A single process serves ~10–15 recognitions/second
on a 1 vCPU box with the shipped bank; scale out with more uvicorn workers or replicas.

## Layers

| Layer | Responsibility | Rules |
|---|---|---|
| `app/api/` | HTTP concerns: parsing, status codes, auth dependencies | Never touches the ORM directly beyond the injected session; raises `AppError` subclasses |
| `app/services/` | Business rules: ingestion, analytics, storage, audit, webhooks | The only place that writes captures; composes ALPR + persistence + policy |
| `app/alpr/` | Pure vision/OCR: no DB, no HTTP, deterministic | `recognize(bytes) -> RecognitionResult` |
| `app/models/` | SQLAlchemy 2.0 declarative entities | All tenant rows carry `org_id`; every query filters on it |
| `app/schemas/` | Pydantic v2 contracts | Drive both validation and OpenAPI |

## Data model

```
Organization 1─┬─* User            (role, zone, scrypt hash)
               ├─* Camera          (code, corridor, status, lanes, fps)
               ├─* Capture         (plate, confidence, status, risk, evidence keys)
               ├─* WatchlistEntry  (plate, category, severity, hits)
               ├─* WebhookEndpoint (url, secret, events, failure_count)
               ├─* ApiKey          (prefix, hash, scopes)
               └─* AuditEvent      (actor, action, entity, detail, ip, request_id)
```

`Capture` indexes `(org_id, created_at)` and `(org_id, plate)`; plate lookups are normalised
(`[^A-Z0-9]` stripped) so `MH 12 AB 3456`, `mh12ab3456` and `MH12AB3456` are one identity.

## Ingestion transaction

1. `validate_upload` (size, emptiness) → `imaging.load_image` (decode, orient, RGB).
2. Persist original + derived thumbnail; compute a 64-bit average hash for near-duplicate detection.
3. `ALPREngine.recognize` → best reading + alternates + per-glyph probabilities.
4. Watchlist lookup: exact match first, then ±1 character tolerance for plates ≥6 chars.
5. Risk scoring (`grammar.risk_score`): watchlist severity, confidence, hour-of-day, duplicate-in-window.
6. Status policy: `flagged` on watchlist hit; `verified` when confidence ≥ review threshold **and**
   the jurisdiction format validates; otherwise `pending`.
7. Write capture, bump watchlist hit counters, audit event, dispatch webhooks.

## Recognition detail

**Detection.** Three proposers plus a flipped pass:

* `morph` — Sobel-X → Otsu → rectangular closing tuned to plate aspect → contours → min-area rect.
* `mser` — maximally stable extremal regions grouped into text lines (handles skew/lighting).
* `contour` — Canny → polygon approximation (4 vertices) → convexity/fill checks (handles frames).
* Fusion — padded variants (±4 %, ±9 %) and unions of overlapping boxes let the reader choose a
  hypothesis instead of trusting one detector; IoU 0.88 keeps near-duplicates by design.

Each proposal is scored by a **plate-likeness verifier**: binarise both polarities, cluster
blobs into a text line, reward population + height consistency + horizontal fill. This rejects
grilles, signs and number stickers before OCR runs.

**Segmentation.** The crop is resized to 96 px height, contrast-limited-adaptive-equalised,
Otsu-thresholded in both polarities, and the polarity whose components look like characters
wins. Components are clustered by height/centre; a light filter removes frame remnants and
isolated fragments. Merging is deliberately conservative — plate characters sit 1–2 px apart,
so any gap heuristic that fuses them costs whole characters.

**Classification.** Each glyph is normalised to 32×48 (aspect preserved, centred). Scores:

```
logit(c) = 0.58 · mean(top-4 template correlations)   # 3 240 rendered templates
         + 0.42 · HOG prototype similarity            # 4×6 cells, 8 bins, 2×2 blocks
         + structural bonus/penalty                   # holes, aspect, ink density
```

softmax at τ = 0.055 yields a distribution, not just a label — which is what the decoder needs.

**Grammar decoding.** For each jurisdiction pattern (e.g. `AA DD L NNNN`), an edit-distance DP
aligns glyph distributions to pattern positions with costs `-log P(char | glyph)` for matches
and fixed penalties for skipped blobs/positions, normalised by sequence length. Patterns are
rejected when longer than the blob count + 2. The best normalised cost wins, with a small
prior favouring high-volume municipal formats.

## Multi-tenancy & authorisation

* Every request resolves a `User` (JWT cookie, bearer token, or `X-API-Key`) whose `org_id`
  scopes all queries. There is no cross-tenant read path.
* Roles: `viewer` (read-only), `operator` (review), `supervisor` (+ delete evidence, watchlist
  writes), `admin` (+ keys, webhooks, cameras, audit).
* The audit log records actor, action, entity, detail, IP and request id for every mutation.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| Unreadable image | `422 invalid_image`, nothing persisted |
| No plate found | Capture still stored with `plate = null`, `status = pending` (evidence preserved) |
| Watchlist lookup error | Ingestion continues; warning logged |
| Webhook failure | Retried with backoff, `failure_count` incremented, never blocks the response |
| Database down | `/api/v1/health` reports `degraded`; `/healthz` keeps the container alive for restarts |
| Storage down | Health reports `error`; uploads fail closed with `500 engine_error` |

## Performance notes

* Glyph bank: built once (~3 s with six faces), cached to `storage/models/glyphbank-*.npz`,
  loaded in ~200 ms. The app warms it during startup so the first request is fast.
* Steady-state recognition on a 1000×620 frame: ~40 ms single-hypothesis, ~110 ms typical
  (multi-hypothesis), p95 ~380 ms on the degraded corpus, on one shared vCPU.
* SQLite in WAL mode is fine for a single node (100+ writes/s). Postgres for anything else.
* Images are served with `Cache-Control: private, immutable`; thumbnails cut bandwidth ~8×.
