# Universal Media Fetcher

A capability-aware media identification service. Paste a URL from any of 21
platforms or a direct media link; it reports what that source is, and **which
operations are actually authorised** for it.

The design premise is that a downloader which says "unsupported" is less useful
than one that says *why*. A Netflix link is not an invalid URL, a DRM-protected
stream is not a network error, and YouTube does not permit downloads. The
capability engine exists to state that distinction per URL.

## Status

**Phase 0 — foundation.** Identification and capability reporting are complete
and tested. No outbound requests are made by any endpoint yet.

| Phase | Scope | Status |
|---|---|---|
| 0 | Identification, SSRF policy, capability profiles | **done** |
| 1 | Direct media, Internet Archive, PeerTube, download manager | planned |
| 2 | Metadata adapters (YouTube, Vimeo, Twitch, Reddit, …) | planned |
| 3 | OTT recognition, TMDB metadata | planned |
| 4 | Accounts, platform connections | planned |
| 5 | Frontend, deployment | planned |

`GET /api/v1/info` reports the same thing in the API, and
`GET /api/v1/platforms` lists the endpoints that do not exist yet. This is
deliberate: §16 of the specification forbids fake success, and a stub that
returns a plausible title would be exactly that.

## Platform support

Only 4 of 21 platforms can download anything, and two of those only for content
the user owns. This table is the honest version; `docs/00-feasibility.md`
explains each row.

| Capability | Platforms |
|---|---|
| Download | Internet Archive, PeerTube |
| Conditional download | Vimeo (own videos), Dailymotion (some) |
| Metadata only | YouTube, Twitch, Reddit, Pinterest, X, TikTok, Instagram, Facebook |
| Recognition + official link | All 9 OTT services |
| Download (media types) | MP4, WebM, MOV, MKV, HLS, DASH, public servers |

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
PYTHONPATH=. .venv/bin/python -m uvicorn umf.main:app --reload
```

Then:

```bash
curl -s localhost:8000/api/v1/platforms | jq '.count'
curl -s -X POST localhost:8000/api/v1/analyze \
  -H 'content-type: application/json' \
  -d '{"url":"https://www.netflix.com/title/80100172"}' | jq
```

```json
{
  "platform": "netflix",
  "platform_name": "Netflix",
  "content_id": "80100172",
  "capabilities": {
    "metadata": false,
    "official_playback": true,
    "authorized_download": false,
    "drm_protected": true
  },
  "metadata_status": "unavailable",
  "metadata_note": "Metadata is not available for this source (DRM_PROTECTED). ..."
}
```

And the refusal path:

```bash
curl -s -X POST localhost:8000/api/v1/analyze \
  -H 'content-type: application/json' \
  -d '{"url":"http://169.254.169.254/latest/meta-data/"}'
# 400 {"error":{"code":"INVALID_URL","message":"The supplied URL is invalid."}}
```

## Endpoints

| Method | Path | Status |
|---|---|---|
| `GET` | `/api/v1/platforms` | live |
| `POST` | `/api/v1/analyze` | live |
| `POST` | `/api/v1/analyze/batch` | live |
| `GET` | `/api/v1/info` | live |
| `GET` | `/healthz` | live |
| `POST` | `/api/v1/downloads` | Phase 1 |
| `GET` | `/api/v1/media/{id}` | Phase 2 |

## Security

The SSRF policy is built before anything fetches a URL, and tested against the
attacks it exists to stop:

- **IP range blocking** across loopback, RFC 1918, link-local, CGNAT, multicast,
  reserved, IPv6 ULA, and IPv4-mapped IPv6 (`::ffff:169.254.169.254`).
- **Non-canonical numeric hosts** (`127.1`, `2130706433`, `0x7f000001`) are
  refused at the syntax stage, because resolvers accept them and `ipaddress`
  does not.
- **DNS resolution requires every answer to be public**, which removes the
  rebinding race rather than trying to win it.
- **Redirects are followed manually**, re-validating each hop, so a public URL
  that 302s to the metadata endpoint is refused before it is fetched.
- **Scheme, port, credential and control-character** checks.

`tests/test_ssrf.py` holds 61 cases covering exactly these.

## Tests

```bash
PYTHONPATH=. pytest tests      # 196 passed
ruff check umf tests
```

Suite layout:

| File | Covers |
|---|---|
| `test_ssrf.py` | hostile URL corpus, DNS rebinding, redirect bypass |
| `test_detection.py` | every platform's URLs; no adapter shadowed |
| `test_capabilities.py` | profiles are honest; refusals carry the right code |
| `test_api.py` | endpoints, status codes, per-URL batch isolation |
| `test_architecture.py` | core contains no platform logic |

The sabotages that must break the suite were verified: disabling the IP range
check, removing redirect re-validation, and restoring the adapter-shadowing bug
fail 53 tests between them.

## Layout

```
umf/
  core/        detector, registry, capability engine, contracts
  adapters/    one class per platform; direct media, hosting, social, OTT
  security/    ssrf policy
  api/         routes
tests/
docs/          feasibility, architecture, roadmap
```

Platform-specific behaviour lives only in `umf/adapters/`. `test_architecture.py`
parses the AST of every core module and fails if a platform name or an adapter
import appears there.
