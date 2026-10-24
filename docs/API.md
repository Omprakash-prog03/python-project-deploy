# API cookbook

Base URL: `/api/v1` · Interactive docs: `/docs` · Schema: `/openapi.json`

All timestamps are UTC ISO-8601. Errors share one envelope:

```json
{"error": {"code": "rate_limited", "message": "Rate limit exceeded…",
           "request_id": "9f2c1a77b3d0", "details": {"retry_after_seconds": 3.4}}}
```

## Authentication

```bash
# Password login → sets pv_session (HttpOnly) and returns a bearer token
curl -c cookies.txt -X POST http://localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"elara@platevision.io","password":"nightwatch"}'

TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"elara@platevision.io","password":"nightwatch"}' | jq -r .access_token)
```

Three credential types are accepted on every endpoint: the `pv_session` cookie, an
`Authorization: Bearer <jwt>` header, or `X-API-Key: pv_…` (organisation-scoped, ideal for
edge devices that cannot store a password).

```bash
curl -X POST http://localhost:8000/api/v1/auth/refresh -b cookies.txt   # rotate session
curl -X POST http://localhost:8000/api/v1/auth/logout   -b cookies.txt
```

## Recognition

```bash
# stateless read (nothing persisted)
curl -s -X POST http://localhost:8000/api/v1/recognize \
  -H "Authorization: Bearer $TOKEN" \
  -F file=@frame.jpg -F country=IN -F include_overlay=true | jq '.best.plate, .best.confidence'

# annotated JPEG (bounding box + glyph boxes + engine footer)
curl -s -X POST http://localhost:8000/api/v1/recognize/annotated \
  -H "Authorization: Bearer $TOKEN" -F file=@frame.jpg -o annotated.jpg

# deterministic demo frames
curl -s -X POST "http://localhost:8000/api/v1/recognize/samples/sample-3" \
  -H "Authorization: Bearer $TOKEN" | jq '{plate: .best.plate, ms: .processing_ms}'
```

`best.format_valid` tells you whether the string satisfies a known jurisdiction pattern — a
cheap, strong signal for whether to auto-clear or queue for review.

## Captures

```bash
# ingest with enforcement
curl -s -X POST http://localhost:8000/api/v1/captures \
  -H "Authorization: Bearer $TOKEN" \
  -F file=@frame.jpg -F camera_id=$CAMERA -F source=upload \
  -F notes="Gate 2 inbound" | jq '{plate, status, risk_level, watchlist_hit}'

# search + filters
curl -s -G http://localhost:8000/api/v1/captures \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode q=MH12 --data status=pending --data risk=high \
  --data sort=confidence --data page=1 --data page_size=20 | jq '.total, .items[0].plate'

# review workflow
curl -s -X PATCH http://localhost:8000/api/v1/captures/$ID \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"status":"verified","notes":"Confirmed against FASTag ledger"}'

# correct a misread
curl -s -X PATCH http://localhost:8000/api/v1/captures/$ID \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"plate":"MH12AB3456"}'

# re-run inference on the stored original (after a bank/grammar upgrade)
curl -s -X POST http://localhost:8000/api/v1/captures/$ID/reanalyze -H "Authorization: Bearer $TOKEN"

# audited export
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/captures/export.csv?date_from=2026-10-01T00:00:00Z" -o capture.csv
```

## Watchlist

```bash
curl -s -X POST http://localhost:8000/api/v1/watchlist \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"plate":"KA05MJ4412","label":"Insurance lapsed","category":"other",
       "severity":"medium","reason":"Policy expired 3 Oct"}'

curl -s "http://localhost:8000/api/v1/watchlist?category=stolen&active_only=true" \
  -H "Authorization: Bearer $TOKEN" | jq 'length'

curl -s http://localhost:8000/api/v1/watchlist/matches -H "Authorization: Bearer $TOKEN" | jq
curl -s -X PATCH http://localhost:8000/api/v1/watchlist/$ID/toggle -H "Authorization: Bearer $TOKEN"
```

Matching is normalised and tolerant: for plates of six or more characters a single substitution
still triggers, and the read is marked `flagged` with the entry's severity folded into the risk
score. `supervisor` or `admin` is required to write.

## Analytics

```bash
curl -s "http://localhost:8000/api/v1/analytics/overview?range=7d&tz_offset=330" \
  -H "Authorization: Bearer $TOKEN" | jq '.kpis'

curl -s "http://localhost:8000/api/v1/analytics/heatmap?days=14" \
  -H "Authorization: Bearer $TOKEN" | jq '.cells[0]'
```

`range` ∈ `24h|7d|30d|90d`; `tz_offset` is minutes east of UTC and shifts the series buckets.

## Cameras

```bash
curl -s -X POST http://localhost:8000/api/v1/cameras \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"code":"NH16-TOLL-02","name":"NH-16 Toll 02","corridor":"NH-16 Coastal","lanes":4,"fps":30}'

curl -s http://localhost:8000/api/v1/cameras/health -H "Authorization: Bearer $TOKEN" | jq
curl -s -X PATCH "http://localhost:8000/api/v1/cameras/$ID/status?status=degraded" \
  -H "Authorization: Bearer $TOKEN"
```

## Operations

```bash
curl -s http://localhost:8000/api/v1/health | jq        # db / storage / engine checks
curl -s http://localhost:8000/healthz                    # liveness (no auth)
curl -s http://localhost:8000/readyz                     # readiness (no auth)
curl -s http://localhost:8000/api/v1/metrics             # Prometheus text format
curl -s http://localhost:8000/api/v1/info -H "Authorization: Bearer $TOKEN" | jq '.usage'
curl -s http://localhost:8000/api/v1/audit -H "Authorization: Bearer $TOKEN" | jq '.[0]'
```

## Integrations

```bash
# API key (plaintext returned once)
curl -s -X POST http://localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"Toll edge","scopes":["captures:read","captures:write"]}'

# webhook subscription
curl -s -X POST http://localhost:8000/api/v1/webhooks \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"url":"https://ops.example.com/hooks/plates","events":["watchlist.hit"],
       "description":"Control room alerting"}'
```

Delivery payload:

```json
{
  "event": "watchlist.hit",
  "sent_at": "2026-10-24T18:41:02+00:00",
  "data": {"capture_id": "…", "plate": "MH12AB3456", "confidence": 0.86,
           "status": "flagged", "risk_level": "critical",
           "category": "stolen", "severity": "critical", "label": "Stolen · Honda City"}
}
```

Headers: `X-PlateVision-Event`, `X-PlateVision-Signature: sha256=<hmac>`. Verification example
is in the README.

## Rate limits & quotas

Token buckets are keyed by API key or client IP with separate buckets per route. Recognition and
ingest cost 3 tokens; everything else costs 1. Exceeding a bucket returns `429 rate_limited` with
`details.retry_after_seconds`. Every response carries `X-Request-ID` and `X-Response-Time`.

Organisation quotas (`monthly_scan_quota`) are reported through `/info`; enforcement is left to
your billing layer so a quota breach never drops evidence mid-shift.
