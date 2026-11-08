# Roadmap

§16 asks for implemented vs planned to be separated. This is that separation,
written before the first line exists.

## Status today

| Phase | Status |
|---|---|
| 0 — Foundation | **not started** |
| 1 — Retrieval core | not started |
| 2 — Metadata adapters | not started |
| 3 — OTT | not started |
| 4 — Platform connections | not started |
| 5 — Production | not started |

**Nothing is implemented.** This repository currently contains PlateVision
(a plate-recognition service); the documents in this folder are the design for
a separate application that has not been built yet.

---

## Phase 0 — Foundation (1–2 days)

The skeleton, with the security layer done properly first rather than bolted on.

- FastAPI app, settings, structured logging, error taxonomy.
- `UrlDetector` + `PlatformResolver` + adapter registry, ~20 platforms
  recognised by host and URL pattern.
- `CapabilityEngine` with the refusal codes, returning honest profiles for
  every platform in the matrix — including the nine OTT entries that report
  `official_playback` only.
- **`ssrf_protection.py`**, built and tested against a hostile corpus *before*
  anything fetches a URL.
- `GET /api/v1/platforms`, `POST /api/v1/analyze` (recognition only).
- Tests: URL tables per platform, plus SSRF vectors.

**Exit criterion:** a pasted URL is correctly identified, and a pasted
`http://169.254.169.254/` is rejected. No network fetches yet.

---

## Phase 1 — Retrieval core (3–5 days)

The group that genuinely works, done end to end.

- `direct_media` adapter: MP4/WebM/MOV/MKV, `Range` resume, size verification.
- `archive_org` adapter: search metadata, format list, real file sizes.
- `peertube` adapter: instance API, download where permitted.
- `DownloadManager`: queue, measured progress, cancel, retry, cleanup.
- HLS and DASH: manifest parse → segment fetch → FFmpeg remux.
- Celery + Redis worker.

**Exit criterion:** paste an Internet Archive URL, get correct metadata, three
real formats with real byte counts, and a working download whose progress
matches bytes on disk — plus a cancelled job that leaves nothing behind.

This is the phase that proves the product is real. Everything after it is
narrower.

---

## Phase 2 — Metadata adapters (3–4 days)

Read-only integrations, with download refused correctly.

- `youtube`: Data API v3 metadata; `authorized_download: false` with reason.
- `vimeo`: API v3 metadata; download only where the token is the owner's.
- `twitch`, `reddit`, `pinterest`, `dailymotion`: metadata.

**Exit criterion:** each adapter returns metadata for a live URL **and** a
correct `DOWNLOAD_NOT_PERMITTED` refusal with tests. Needs API keys; without
them the adapter reports `AUTHENTICATION_REQUIRED` rather than failing.

---

## Phase 3 — OTT (2–3 days)

Recognition and honest limitation, which is all that is available.

- The nine OTT adapters: URL recognition, content ID, official deep link.
- TMDB integration for title/poster/provider availability by region.
- UI: the §8 OTT screen — provider, subscription note, official link, explicit
  explanation of why there is no download.

**Exit criterion:** a Netflix URL yields the title, the poster, "available on
Netflix India", an official link, and a clear DRM explanation. No download
button exists, and the API returns `DRM_PROTECTED`, not `INVALID_URL`.

---

## Phase 4 — Accounts and connections (2–3 days)

- Registration, login, JWT sessions.
- `PlatformConnection` with encrypted OAuth refresh tokens — enabling the
  Vimeo "download my own videos" path for real.
- Per-user history and quotas.

**Exit criterion:** connect a Vimeo account via OAuth, download a video from it,
and confirm the token is unreadable at rest and absent from logs.

---

## Phase 5 — Production (3–4 days)

- Frontend build-out to §8, light and dark.
- Docker, compose, environment config, migrations.
- Load test on the download path; SSRF test suite re-run.
- Docs, deployment, and an honest capability matrix published in the UI.

---

## Sequencing rationale

The spec orders phases §14 as Foundation → Metadata → Retrieval → OTT. I have
**swapped Metadata and Retrieval**, which is a small change with a large
effect:

- Retrieval is where the hard, differentiating work is (streaming, resume,
  progress, verification, SSRF under real load). Proving it early de-risks the
  project.
- Metadata adapters are mostly HTTP + JSON against well-documented APIs, and
  several need API keys and review processes that take longer than the code.
  Starting them first would block on paperwork.
- A product that downloads perfectly and shows thin metadata is credible. One
  that shows beautiful metadata and cannot download is the failure mode the
  spec warns against in §16.

---

## Deliberately out of scope

| Item | Why |
|---|---|
| yt-dlp / stream extraction | Contradicts §2 and §11 |
| Login-wall scraping, cookie import | Contradicts §11 |
| DRM handling of any kind | Explicitly excluded by §2 |
| Platforms beyond the §2 list | §16 prefers reliability over coverage |
| Browser extension | Not requested; large surface |

---

## Open questions

1. **Where does this live?** It is a separate application from PlateVision in
   this repository. It should probably be its own repository, or a top-level
   folder that can be split out cleanly.
2. **Which API keys exist?** YouTube (free), Vimeo (free OAuth app), TMDB
   (free), Reddit (free), X (paid), Dailymotion (free). The matrix is much
   richer with the free ones.
3. **Is the OTT section worth its cost?** Nine adapters producing link-outs.
   I would build it, because recognising a Netflix URL correctly is a real
   user expectation — but it is the lowest value per line of code in the plan.
