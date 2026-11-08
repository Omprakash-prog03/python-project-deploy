# Feasibility: what can actually be built

This is the answer to §16 questions 1–4 and 6, written before any code.

The short version: **the spec's platform list is achievable, but not with the
operations it assumes.** Of the 21 named platforms, two support unconditional
download, two support conditional download, seven are metadata-only, and nine
cannot be integrated beyond URL recognition at all.

That is not a reason to abandon the project. It is a reason to build a
capability-aware system — which §1 and §5 already ask for. The capability
engine is not a nice-to-have that reports "download: true" for everything; it
is the feature that makes the product honest.

---

## 1. The rules that constrain every adapter

Three constraints decide what each adapter can do. They are independent, and a
platform fails at least one of them almost always.

| Constraint | Means | Consequence |
|---|---|---|
| **No API** | The platform publishes no content endpoint for third parties | Nothing can be retrieved at all; not even a title |
| **No authorization** | An API exists, but only for content you own or manage | Public URLs cannot be resolved; only your own library |
| **No permission** | Files are technically fetchable, but the terms forbid it | Must not be implemented, even where it would work |

Breaking the third is not a technical risk, it is a legal one, and it is the
reason the popular "universal downloader" category is mostly built on ToS
violations. §2 asks for "authorized mechanisms" only, so this document applies
that test consistently — including where it costs us platform coverage.

---

## 2. Platform feasibility matrix

Legend: **✅ supported** · **◐ conditional** · **○ metadata only** · **✗ not possible**

### A. Video hosting

| Platform | Official API | Metadata | Download | Verdict |
|---|---|---|---|---|
| **YouTube** | Data API v3, free, 10k units/day | ✅ | ✗ | ○ Metadata + captions for owned videos |
| **Vimeo** | API v3, OAuth 2.0 | ✅ | ◐ | ◐ Download **own videos only** |
| **Dailymotion** | Public API | ✅ | ◐ | ◐ Some uploads expose direct URLs |
| **Twitch** | Helix API | ✅ | ✗ | ○ Metadata for VODs/clips + embed |
| **Internet Archive** | Public JSON API | ✅ | ✅ | ✅ **Full download, no restriction** |
| **PeerTube** | Public REST API | ✅ | ✅ | ✅ Where the instance permits it |

**YouTube.** Data API v3 has no download endpoint. Its ToS: *"You shall not
download any Content unless you see a 'download' or similar link displayed by
YouTube on the Service."* The only file-bearing endpoint is
`captions.download`, and it works solely for videos the authenticated user
owns. So YouTube is metadata + optional captions, and the capability response
says `authorized_download: false` with a reason.

**Vimeo.** This is the subtlest one and worth stating precisely, because it
recurs in every "Vimeo download API" tutorial. The video object has a
`download` key — **but it is only returned when the bearer token belongs to the
video's owner *and* the API app was created by that same owner.** The `files`
key behaves similarly. There is no token a third-party service can hold that
unlocks downloads for another user's public videos. So: metadata for public
videos; download for the owner's own library. That is a real, useful feature —
it is just not "download any Vimeo link".

**Internet Archive.** The best integration in the list. Public metadata API,
explicitly downloadable items, formats enumerated per item, real file sizes.
This should be the flagship adapter.

### B. Social media

| Platform | Official API | Metadata | Download | Verdict |
|---|---|---|---|---|
| **Instagram** | Graph API, app review, business accounts | ◐ own only | ✗ | ○ Own account only |
| **Facebook** | Graph API, same gating | ◐ own only | ✗ | ○ Own account only |
| **X (Twitter)** | API v2, paid tiers | ◐ paid | ✗ | ○ Metadata on a paid key |
| **Reddit** | OAuth API + public JSON | ✅ | ✗ | ○ Metadata + link out |
| **TikTok** | Display API, needs approval | ◐ own only | ✗ | ○ Own account only |
| **Pinterest** | API v5 | ✅ | ✗ | ○ Metadata + link out |

**Instagram / Facebook.** Two hard facts. The Basic Display API was **shut down
on 4 December 2024**, so there is no public read path at all. The Graph API
serves only accounts that granted your app permission, requires Business or
Creator accounts linked to a Page, and needs app review taking 2–4 weeks per
round. An arbitrary public Instagram URL cannot be resolved through any
supported route. Anything that does resolve it is scraping behind a login
wall — which the spec forbids.

**X.** Metadata is reachable but the API is metered and expensive; budgets
matter more than code here.

### C. OTT and streaming — the honest section

| Platform | Public content API | Metadata | Download |
|---|---|---|---|
| Netflix | **None since 2014** | ✗ | ✗ |
| Amazon Prime Video | None | ✗ | ✗ |
| JioHotstar | None | ✗ | ✗ |
| Sony LIV | None | ✗ | ✗ |
| ZEE5 | None | ✗ | ✗ |
| Apple TV+ | None | ✗ | ✗ |
| Hulu | None | ✗ | ✗ |
| Max | None | ✗ | ✗ |
| Paramount+ | None | ✗ | ✗ |

**Netflix closed its public API in 2014 and has not reopened it.** None of the
other eight publish a content metadata API either. What exists is the opposite
kind of API — partner integrations for the platform's own apps.

§2 asks for "official authentication, API integrations, metadata discovery,
account-linked access where supported, and authorized offline-download
workflows". For these nine services, **none of that is available to a
third-party application.** Their offline downloads are a feature *inside their
own apps*, tied to their own DRM, and they expose no API to hand a file to
anyone else. §2 also says not to exclude OTT because of DRM — agreed, and the
distinction is worth preserving: the blocker here is not DRM, it is the
**absence of an API**, which no amount of DRM handling would fix.

There are two things an OTT adapter can legitimately do:

1. **Recognise the URL** and report the platform, content identifier, and an
   authoritative deep link to the official app.
2. **Resolve the title** through a licensed metadata aggregator rather than the
   platform. **TMDB** is free for non-commercial use and carries
   watch-provider data per region; **Watchmode** and **Reelgood** are paid and
   cover availability more deeply. This yields "this title is on Netflix in
   India", which is genuinely useful and entirely legitimate.

So the OTT "interface" in §8 becomes: title, poster, provider, region
availability, subscription note, and a button that opens the official app. No
download button, because there is nothing truthful to put behind one.

### D. Direct media — the group that fully works

| Source | Download | Notes |
|---|---|---|
| MP4 / WebM / MOV / MKV | ✅ | Progressive download, resumable via `Range` |
| HLS (`.m3u8`) | ✅ | Parse manifest, fetch segments, remux with FFmpeg |
| MPEG-DASH (`.mpd`) | ✅ | Same, different manifest grammar |
| Public media servers | ✅ | Subject to the SSRF rules in §11 |

This is where most of the product's real value sits, and where "every button
must perform its actual intended operation" is achievable without compromise.
It is also the only group where download can be *unconditionally* supported —
which makes it the right Phase 1.

---

## 3. Scorecard

| Category | Count | Platforms |
|---|---|---|
| Full download | **2** | Internet Archive, PeerTube |
| Conditional download | **2** | Vimeo (own), Dailymotion (some) |
| Metadata only | **8** | YouTube, Twitch, Vimeo public, Reddit, Pinterest, X, TikTok, Instagram/Facebook (own) |
| Recognition + deep link only | **9** | All OTT |
| Full download (media types) | **7** | MP4, WebM, MOV, MKV, HLS, DASH, public servers |

**Four of the 21 named platforms can download anything, and two of those only
for content the user owns.** The rest do something narrower but real. The
system must say which is which, per URL, before the user commits — which is
exactly what §5's capability engine is for.

---

## 4. What this means for the spec's assumptions

Four places where the spec and reality diverge, and the decision taken in each:

| § | Spec assumes | Reality | Decision |
|---|---|---|---|
| §2 | OTT metadata via platform APIs | No APIs exist | TMDB/Watchmode for metadata; platform for deep link only |
| §3 | "Media type" and "publicly accessible" per URL | Undeterminable for OTT without an API | Report `UNKNOWN` honestly rather than guessing |
| §6 | Download queue for all sources | 4 of 21 platforms + direct media | Queue exists; unsupported sources return `DOWNLOAD_NOT_PERMITTED` with a reason |
| §15 | Test every adapter for download | Most cannot download | Test each adapter against **its actual capability profile**, including correct refusal |

§15 says an adapter is complete only when its supported operations are tested.
That is the right bar, and it is achievable — as long as "supported operations"
means what the platform actually permits, not what the spec hoped for.

---

## 5. Legal posture

The system will implement only operations a platform authorises. Concretely:

- No DRM circumvention, and no integration that removes encryption.
- No credential harvesting, session-cookie extraction, or token theft.
- No bypassing paywalls, geo-blocks, or login walls.
- No scraping in violation of terms, including via headless browsers.
- Where an operation is refused, the API returns a machine-readable reason and
  a legitimate alternative (open in the app, watch officially, contact the
  rights holder).

This deliberately excludes the largest reason people want such a tool. It is
also the only version that can be operated publicly without being shut down or
sued, and it is what §2 asks for in writing.
