"""ParkPow / Plate Recognizer cloud backend.

`parkpow/deep-license-plate-recognition` is often mistaken for a self-contained
model. It is not: the repository holds no weights and no inference code. It is a
client that ships an image to Plate Recognizer's hosted API and returns their
answer, or a launcher for their proprietary ``platerecognizer/alpr`` container,
which needs a licence key of its own.

So "adding that project" here means adding an HTTP client, and this module is
that client, written against the same request the upstream CLI makes::

    POST https://api.platerecognizer.com/v1/plate-reader/
    Authorization: Token <API key>
    files: upload=<jpeg>
    data:  regions=in  camera_id=...

The important design rule is that this backend must never be able to take the
service down. Every failure mode -- no key, timeout, 5xx, malformed JSON, an
empty result -- raises :class:`RemoteEngineError`, which
:class:`app.alpr.engine.FallbackEngine` catches so the local OpenCV pipeline
answers instead. A deploy with no key configured simply runs locally.
"""
from __future__ import annotations

import contextlib
import io
import time
from typing import Any

import httpx

from app.alpr import grammar
from app.alpr.types import Box, PlateReading, RecognitionResult

DEFAULT_BASE_URL = "https://api.platerecognizer.com"
READER_PATH = "/v1/plate-reader/"
ENGINE_NAME = "parkpow"
# Retry only on conditions that are somebody else's transient problem.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_BACKOFF_SECONDS = 8.0


class RemoteEngineError(RuntimeError):
    """A remote read failed. Callers fall back to the local engine."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _box_from(raw: Any) -> Box:
    """Plate Recognizer reports an inclusive xmin/ymin/xmax/ymax rectangle."""
    if not isinstance(raw, dict):
        return Box(0, 0, 1, 1)
    xmin = int(_as_float(raw.get("xmin")))
    ymin = int(_as_float(raw.get("ymin")))
    xmax = int(_as_float(raw.get("xmax")))
    ymax = int(_as_float(raw.get("ymax")))
    return Box(xmin, ymin, max(1, xmax - xmin), max(1, ymax - ymin))


def _country_from_region(raw: Any) -> tuple[str, str | None]:
    """Map ``{"code": "in"}`` (or ``"us-ca"``) onto our country vocabulary."""
    code = raw
    if isinstance(raw, dict):
        code = raw.get("code") or raw.get("value")
    if not code or not isinstance(code, str):
        return "GENERIC", None
    code = code.strip().lower()
    if not code:
        return "GENERIC", None
    known = {fmt.country.lower() for fmt in grammar.FORMATS}
    head = code.split("-")[0]
    country = head.upper() if head in known else "GENERIC"
    return country, code


def _plate_value(item: dict[str, Any]) -> tuple[str, float, Any]:
    """Pull the plate string, its detection score and its box.

    Two response shapes are in circulation: a flat one where ``plate`` is a
    string, and a nested one where it is ``{"value": ..., "box": ..., "score":
    ...}``. Both are accepted.
    """
    plate = item.get("plate")
    if isinstance(plate, dict):
        text = plate.get("value") or plate.get("plate") or ""
        score = _as_float(plate.get("score", item.get("dscore")))
        box = plate.get("box", item.get("box"))
    else:
        text = plate or item.get("value") or ""
        score = _as_float(item.get("dscore", item.get("score")))
        box = item.get("box")
    return str(text), score, box


def _candidates(item: dict[str, Any]) -> list[dict[str, Any]]:
    raw = item.get("candidates")
    if isinstance(raw, dict):
        raw = raw.get("plate") or []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for cand in raw:
        if not isinstance(cand, dict):
            continue
        value = cand.get("plate") or cand.get("value")
        if not value:
            continue
        out.append({"plate": str(value), "confidence": round(_as_float(cand.get("score")), 4)})
    return out


def parse_response(payload: Any, *, elapsed_ms: float, width: int, height: int) -> RecognitionResult:
    """Turn a Plate Recognizer payload into our result object.

    Kept separate from the transport so it can be tested without a network.
    """
    if not isinstance(payload, dict):
        raise RemoteEngineError("response was not a JSON object")
    results = payload.get("results")
    if results is None:
        results = []
    if not isinstance(results, list):
        raise RemoteEngineError("response 'results' was not a list")

    plates: list[PlateReading] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        text, score, raw_box = _plate_value(item)
        text = grammar.normalize(text)
        if not text:
            continue
        country, region_code = _country_from_region(item.get("region"))
        fmt_valid = any(
            grammar.validate(text, fmt) for fmt in grammar.formats_for(country) if fmt.country != "GENERIC"
        )
        plates.append(
            PlateReading(
                plate=text,
                raw_text=text,
                confidence=round(max(0.0, min(1.0, score)), 4),
                box=_box_from(raw_box),
                country=country,
                region=region_code,
                format_name=None,
                format_valid=fmt_valid,
                alternates=_candidates(item)[:3],
                diagnostics={
                    "engine": ENGINE_NAME,
                    "region_code": region_code,
                    "dscore": _as_float(item.get("dscore")),
                    "candidate_score": _as_float(item.get("score")),
                },
            )
        )

    version = str(payload.get("version") or "")
    return RecognitionResult(
        plates=plates,
        image_width=width,
        image_height=height,
        processing_ms=elapsed_ms,
        engine_version=f"{ENGINE_NAME}/{version}" if version else ENGINE_NAME,
        timings={"remote_ms": elapsed_ms},
        diagnostics={
            "engine": ENGINE_NAME,
            "processing_time_ms": _as_float(payload.get("processing_time")),
        },
    )


class ParkPowEngine:
    """Cloud plate reader. Same ``recognize`` contract as the local engine."""

    name = ENGINE_NAME

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 8.0,
        regions: tuple[str, ...] = (),
        camera_id: str | None = None,
        retries: int = 2,
        client: httpx.Client | None = None,
        sleep=time.sleep,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.regions = tuple(r for r in regions if r)
        self.camera_id = camera_id
        self.retries = max(0, retries)
        self._client = client
        self._sleep = sleep
        self.version = f"{ENGINE_NAME}"

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}{READER_PATH}"

    # ------------------------------------------------------------------ http
    def _request(self, image_bytes: bytes) -> dict[str, Any]:
        if not self.available:
            raise RemoteEngineError("no API key configured")

        # httpx repeats a field when the value is a list, which is how the
        # upstream CLI sends several ``-r`` regions.
        data: dict[str, Any] = {}
        if self.regions:
            data["regions"] = list(self.regions)
        if self.camera_id:
            data["camera_id"] = self.camera_id

        files = {"upload": ("image.jpg", image_bytes, "image/jpeg")}
        headers = {"Authorization": f"Token {self.api_key}"}

        last: RemoteEngineError | None = None
        for attempt in range(self.retries + 1):
            try:
                if self._client is not None:
                    response = self._client.post(
                        self.endpoint, files=files, data=data, headers=headers, timeout=self.timeout
                    )
                else:
                    response = httpx.post(
                        self.endpoint, files=files, data=data, headers=headers, timeout=self.timeout
                    )
            except httpx.TimeoutException as exc:
                last = RemoteEngineError(f"timeout after {self.timeout}s: {exc}", retryable=True)
            except httpx.HTTPError as exc:
                last = RemoteEngineError(f"transport error: {exc}", retryable=True)
            else:
                if response.status_code in RETRY_STATUS:
                    last = RemoteEngineError(
                        f"HTTP {response.status_code} from {self.endpoint}",
                        status=response.status_code,
                        retryable=True,
                    )
                    if attempt < self.retries:
                        delay = min(MAX_BACKOFF_SECONDS, 0.5 * (2**attempt))
                        retry_after = response.headers.get("Retry-After")
                        if retry_after:
                            with contextlib.suppress(ValueError):
                                delay = min(MAX_BACKOFF_SECONDS, float(retry_after))
                        self._sleep(delay)
                        continue
                elif response.status_code >= 400:
                    raise RemoteEngineError(
                        f"HTTP {response.status_code} from {self.endpoint}: {response.text[:200]}",
                        status=response.status_code,
                    )
                else:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise RemoteEngineError(f"response was not JSON: {exc}") from exc
                    if not isinstance(payload, dict):
                        raise RemoteEngineError("response was not a JSON object")
                    return payload
            if attempt < self.retries and last is not None and last.retryable:
                self._sleep(min(MAX_BACKOFF_SECONDS, 0.5 * (2**attempt)))
                continue
            break

        raise last or RemoteEngineError("remote read failed")

    # ------------------------------------------------------------------- api
    def recognize(self, image_bytes: bytes, options=None) -> RecognitionResult:
        width, height = _image_size(image_bytes)
        started = time.perf_counter()
        payload = self._request(image_bytes)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return parse_response(payload, elapsed_ms=elapsed_ms, width=width, height=height)


def _image_size(image_bytes: bytes) -> tuple[int, int]:
    """Read the dimensions from the image header without decoding the pixels."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as handle:
            return int(handle.width), int(handle.height)
    except Exception:  # pragma: no cover - a broken upload is rejected upstream
        return 0, 0
