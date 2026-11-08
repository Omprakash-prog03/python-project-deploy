# Architecture

How the pieces fit, and why each boundary is where it is.

```
┌──────────────────────────────────────────────────────────────┐
│  React + TypeScript + Tailwind                              │
│  paste → analyze → capabilities → (download | open official) │
└───────────────────────────┬──────────────────────────────────┘
                            │ REST, JSON
┌───────────────────────────▼──────────────────────────────────┐
│  FastAPI                                                     │
│                                                              │
│  analyze ──► UrlDetector ──► PlatformResolver                │
│                                     │                        │
│                                     ▼                        │
│                          CapabilityEngine ◄── AdapterRegistry │
│                                     │                        │
│              ┌──────────────────────┼──────────────────────┐ │
│              ▼                      ▼                      ▼ │
│        MetadataEngine        DownloadManager        History   │
│              │                      │                       │
│              └──────────┬───────────┘                       │
│                         ▼                                    │
│                   Adapter layer                              │
│     youtube │ vimeo │ archive_org │ peertube │ direct_media  │
│     twitch  │ reddit│ pinterest   │ dailymotion │ ott_link   │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
              PostgreSQL · Redis · Celery · FFmpeg
```

---

## 1. The adapter contract

Every platform is one class implementing one protocol. Nothing about a specific
platform may appear outside its own package — that is the rule from §4, and it
is enforceable with a linter rather than goodwill.

```python
class Adapter(Protocol):
    slug: str                      # "youtube"
    display_name: str              # "YouTube"
    hosts: tuple[str, ...]         # hostnames it claims

    def identify_url(self, url: str) -> IdentifyResult: ...
    async def get_content_metadata(self, ref: ContentRef) -> Metadata: ...
    async def check_authentication(self, ref: ContentRef) -> AuthStatus: ...
    def get_capabilities(self, ref: ContentRef | None = None) -> Capabilities: ...
    async def get_authorized_download_options(self, ref) -> list[DownloadOption]: ...
    async def retrieve_media(self, option: DownloadOption, sink: Sink) -> None: ...
    async def get_subtitles(self, ref: ContentRef) -> list[SubtitleTrack]: ...
    async def get_playback_url(self, ref: ContentRef) -> str | None: ...
```

Two rules make this work in practice:

1. **Capabilities are declarative and static per adapter**, so `GET
   /api/v1/platforms` can answer without touching the network. Per-URL
   refinement happens at analysis time (a Vimeo video is downloadable only if
   the token is the owner's).
2. **Unsupported is a value, not an exception.** A method that cannot act
   returns `Unsupported(reason=..., code=...)`. Exceptions are reserved for
   genuine faults. This is what stops "not supported" from becoming a 500.

### Why this shape

The alternative — one `UniversalDownloader` class with `if platform == ...`
branches — is what the project this replaces would have become. It fails at
the sixth platform, and it makes the capability matrix impossible to keep
honest, because nothing forces a refusal path to be written.

---

## 2. The capability engine

Capabilities are computed from three inputs and returned as one object:

```
adapter.static_capabilities
  ∩ adapter.check_authentication(url)      → does this URL need a login?
  ∩ adapter.check_availability(url)        → is it reachable, regional, expired?
  ∩ policy                                → is this operation permitted at all?
```

Every result carries a machine-readable `reason`:

| Code | Meaning |
|---|---|
| `INVALID_URL` | Malformed or not a media URL |
| `UNSUPPORTED_PLATFORM` | No adapter claims this host |
| `AUTHENTICATION_REQUIRED` | The platform's official auth is needed |
| `SUBSCRIPTION_REQUIRED` | Paywalled at the source |
| `DRM_PROTECTED` | Encrypted stream; not a broken URL |
| `DOWNLOAD_NOT_PERMITTED` | Technically possible, not authorised |
| `TEMPORARY_FAILURE` | Retryable upstream problem |
| `NETWORK_ERROR` | Could not reach the source |
| `UNAVAILABLE` | Removed, private, or geo-blocked |

`DRM_PROTECTED` is deliberately distinct from `INVALID_URL`. A user who pastes
a Netflix link does not want "invalid URL" — they want "this is Netflix, it is
DRM-protected, here is the official page, and here is why there is no download
button." That distinction is *the* product decision in the spec, and it lives
here.

---

## 3. Security: the part that is not optional

A URL-fetching service is an SSRF engine pointed at your own network unless it
is built not to be. §11 lists the requirements; this is how they are actually
met.

### 3.1 SSRF prevention — the core problem

Naive validation checks the URL string. That is insufficient: `http://internal`,
`http://127.0.0.1`, `http://169.254.169.254` (cloud metadata),
`http://[::1]`, and `http://metadata.google.internal` all pass a string check.

The defence is in four stages, and **stage 3 is what actually stops an attack**:

1. **Scheme allowlist** — `http`, `https` only. No `file:`, `gopher:`, `ftp:`.
2. **Port allowlist** — 80, 443, and an explicit opt-in list. Not 22, 6379, 5432.
3. **Resolve, then check, then connect to the resolved IP.** Resolve the
   hostname, reject any answer in a non-public range, then pin the connection
   to *that* IP with `Host:` preserved. This closes DNS rebinding, where a
   hostname resolves to a public IP during validation and a private one
   milliseconds later.
4. **No automatic redirects.** Follow them manually, and re-run stages 1–3 on
   every hop. A public URL that 302s to `169.254.169.254` is the standard
   bypass.

Blocked ranges: loopback, private (RFC 1918), link-local, CGNAT, multicast,
reserved, IPv6 ULA, `::1`, IPv4-mapped IPv6, and `0.0.0.0/8`.

### 3.2 The rest

| Requirement | Implementation |
|---|---|
| Rate limiting | Redis token bucket, per user and per IP |
| File-type verification | Magic bytes, not `Content-Type`; must agree with the sniffed container |
| Size limits | `Content-Length` **and** a live byte counter, so a lying header cannot exhaust disk |
| Timeouts | Connect + read timeouts on every hop; total wall-clock cap per job |
| Redirect safety | Manual follow, re-validated per hop |
| Temp storage | Per-job directory, `0700`, deleted on completion, cancel, and failure |
| Cleanup | Sweeper for orphaned jobs; TTL on anything kept |
| FFmpeg | `subprocess` with an **argument list, never `shell=True`**; protocol allowlist; no `concat:` from untrusted sources |
| Credentials | Platform tokens in a `PlatformConnection` row, encrypted at rest; never in logs |

### 3.3 What is deliberately not built

No yt-dlp, no `--no-check-certificate`, no cookie import, no headless browser
that solves logins. Each would raise platform coverage and each would move the
project into the category the spec explicitly excludes.

---

## 4. Download pipeline

```
POST /downloads ──► validate capability ──► queue job (Celery)
                                                │
       ┌────────────────────────────────────────┘
       ▼
  resolve → stream to temp (.part) → verify → move to storage → serve
       │           │                       │
       │           └─ progress → Redis ────┘
       │                  │
       │                  └─► SSE/WebSocket → UI
       └─ cancel: cooperative flag checked per chunk
```

Design decisions that matter:

- **Progress is measured, not simulated.** Bytes written over elapsed time. If
  the size is unknown (no `Content-Length`), it reports bytes and `null`
  percentage rather than inventing a number. §16 forbids simulated progress,
  and this is where that is easiest to get wrong.
- **Resume via HTTP `Range`** where the server advertises
  `Accept-Ranges: bytes`; otherwise the option is hidden, not offered and then
  broken.
- **Verification before the job is marked complete** — size matches, container
  probes cleanly via `ffprobe`, and for HLS the remux exits 0.
- **Cancellation is cooperative.** A flag checked per chunk, so cancel is
  immediate and leaves no half-file behind.

---

## 5. Data model

As §10, with two changes worth flagging:

- `DownloadJob` gains `bytes_total`, `bytes_done`, `error_code`, `adapter` —
  needed for honest progress and for the failure taxonomy.
- `PlatformConnection` stores **only** an encrypted OAuth refresh token and a
  scope list. No passwords, no session cookies, no device tokens. The spec
  already says this; the schema should make the alternative impossible to
  store by accident.

---

## 6. Stack decisions

| Choice | Why |
|---|---|
| FastAPI + async | Adapters are I/O-bound; the async path is the natural fit |
| Celery + Redis | Downloads outlive requests; separate worker scales independently |
| PostgreSQL | Prod; SQLite for the MVP, same SQLAlchemy models |
| FFmpeg | Only for remux/transcode of *already-authorised* media — HLS→MP4, mp3 extraction where permitted |
| React + TS + Tailwind | §9's stack; the design language is §8's |

**One deliberate departure from §9:** I would not use yt-dlp, despite it being
the obvious library for this domain. It exists to extract streams platforms do
not authorise, so importing it would contradict §2 and §11 in a single line.

---

## 7. What "done" means for an adapter

An adapter ships only when, for its own capability profile:

- valid URLs identify correctly, and invalid ones are rejected;
- every *claimed* capability works against the live platform;
- every *unclaimed* capability returns the right refusal code, not an error;
- its tests include the failure paths from §15 — expired links, private
  videos, auth-required, region-blocked, network failure.

The last point is the one that separates this from a demo. A `youtube` adapter
that returns metadata and correctly refuses download, with tests proving the
refusal, is complete. One that returns metadata and *claims* download it cannot
deliver is not.
