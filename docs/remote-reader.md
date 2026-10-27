# The optional hosted reader

PlateVision reads plates locally with OpenCV: detection, perspective
rectification, glyph segmentation, a template/kNN classifier and a
grammar-constrained decoder. That path needs no network and no credentials, and
it is what runs by default.

This page covers the *other* path — sending an image to
[Plate Recognizer](https://platerecognizer.com) (the company behind the
`parkpow` GitHub organisation) and using their answer instead.

## First, what that repository actually is

`parkpow/deep-license-plate-recognition` is widely assumed to be a self-contained
deep model. It is not, and it is worth being precise about this before planning
around it:

| Check | Result |
|---|---|
| Model weight files (`.h5`, `.pt`, `.onnx`, `.pb`, …) | **0** |
| Training or inference code | **0** |
| Dependencies in `pyproject.toml` | `flask`, `paramiko`, `pillow`, `psutil`, `requests` |
| What `plate_recognition.py` does | `POST`s your image to an HTTP endpoint |

The deep models live on Plate Recognizer's servers, or inside their proprietary
`platerecognizer/alpr` container, which you pull with `docker pull` and run with
a `LICENSE_KEY`. Neither the weights nor the engine are in the repository. The
repository is a **client**, plus integrations for cameras, webhooks and a gate
controller.

So "adding it to this project" means adding an HTTP client, and that is exactly
what `app/alpr/remote.py` is — written against the same request the upstream CLI
makes, so either the cloud API or a self-hosted Snapshot SDK can answer it.

## Enabling it

```bash
ALPR_ENGINE=parkpow
PARKPOW_API_KEY=your-key
PARKPOW_REGIONS=in            # optional, comma separated, sent as -r
PARKPOW_CAMERA_ID=Gate-1      # optional
PARKPOW_TIMEOUT_SECONDS=8
PARKPOW_RETRIES=2
```

To point at a self-hosted container instead of the cloud, set
`PARKPOW_BASE_URL=http://localhost:8080` and leave `PARKPOW_API_KEY` empty —
the Snapshot SDK authenticates by container, not by token.

Check which reader is live:

```bash
curl -s localhost:8000/healthz | jq .checks.engine_backend
# "local"  or  "parkpow"
```

`GET /health` also reports `remote_reader`, which is `on`, `off`, or
`missing-key` when `ALPR_ENGINE=parkpow` was set without a key.

## Why it cannot break the deploy

The remote path is wrapped, not substituted. `FallbackEngine` calls the remote
reader first and catches **any** failure — no key, timeout, transport error,
401, exhausted retries, malformed JSON — then runs the local pipeline and tags
the result `engine: "local"`.

The consequences, which are the reason it is built this way:

- **No key configured → the app is fully local.** A deploy never fails for want
  of a credential, and `render.yaml` leaves the variables commented out.
- **The vendor goes down → reads still succeed**, at local accuracy.
- **A bad key → one 401, no retry storm.** Only 429/5xx are retried, with
  exponential backoff capped at 8 s and `Retry-After` honoured.

A remote answer with zero plates is treated as a real answer and is *not*
re-run locally: the cloud looked and found nothing, and guessing afterwards
would manufacture false positives.

## What you give up

- **Cost.** The hosted API is billed per lookup and rate-limited; the free tier
  is a few thousand calls a month. There is no self-hosted free option.
- **Privacy.** Every image leaves your infrastructure and is processed by a
  third party. For a tolling or law-enforcement deployment, check that against
  your own retention policy before enabling this.
- **Latency.** A round trip replaces a local read that takes ~160 ms at the
  median.
- **Determinism in tests.** The suite exercises the client against a mocked
  transport, so nothing in CI calls the network.

## Tests

`tests/test_remote_engine.py` covers both response shapes in circulation, the
multipart request shape, region/camera fields, rate-limit retry, non-retry of
401, timeout, non-JSON bodies, and the fallback. The three that protect the
deploy are:

- `test_missing_key_fails_before_any_network_call`
- `test_factory_stays_local_without_a_key`
- `test_fallback_returns_the_local_result_when_remote_dies`

Each was verified to fail when the corresponding guard is removed.
