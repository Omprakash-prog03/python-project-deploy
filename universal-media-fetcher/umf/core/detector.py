"""URL detection: turn a pasted string into an identified content reference.

This module performs syntax validation only. It never resolves DNS and never
opens a socket, so it is safe to call on any user input and cheap enough to run
per keystroke if the UI ever wants live feedback. Resolution belongs to the
fetcher (Phase 1), behind the SSRF policy.
"""
from __future__ import annotations

from dataclasses import dataclass

from umf.core.contracts import Adapter, IdentifyResult
from umf.core.models import ContentRef, MediaKind, UrlKind
from umf.core.registry import AdapterRegistry
from umf.core.registry import registry as default_registry
from umf.errors import AppError, ErrorCode
from umf.security.ssrf import ValidatedUrl, validate_url


@dataclass(frozen=True, slots=True)
class Detection:
    """Everything known about a URL before any network call."""

    url: str
    normalized: str
    host: str
    platform: str
    platform_name: str
    content_id: str | None
    url_kind: UrlKind
    media_kind: MediaKind
    confidence: float
    note: str
    adapter: Adapter

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "normalized_url": self.normalized,
            "host": self.host,
            "platform": self.platform,
            "platform_name": self.platform_name,
            "content_id": self.content_id,
            "url_kind": self.url_kind.value,
            "media_kind": self.media_kind.value,
            "confidence": round(self.confidence, 3),
            "note": self.note,
        }


class UrlDetector:
    def __init__(self, registry: AdapterRegistry | None = None) -> None:
        self.registry = registry or default_registry

    def validate(self, url: str) -> ValidatedUrl:
        return validate_url(url)

    def detect(self, url: str) -> Detection:
        validated = self.validate(url)
        match = self.registry.match(validated.normalized)
        if match is None:
            raise AppError(
                ErrorCode.UNSUPPORTED_PLATFORM,
                detail={"host": validated.host},
            )
        adapter, result = match
        assert isinstance(result, IdentifyResult)

        # A shape-only match (PeerTube) is a candidate, not a fact. Say so at
        # identify time instead of pretending certainty the URL cannot provide.
        note = result.note
        if validated.host and adapter.hosts and not any(
            validated.host == h or validated.host.endswith("." + h) for h in adapter.hosts
        ):
            note = note or "matched by URL shape"

        return Detection(
            url=url,
            normalized=validated.normalized,
            host=validated.host,
            platform=adapter.slug,
            platform_name=adapter.display_name,
            content_id=result.content_id,
            url_kind=result.url_kind,
            media_kind=result.media_kind,
            confidence=result.confidence,
            note=note,
            adapter=adapter,
        )

    def detect_many(self, urls: list[str]) -> list[Detection | AppError]:
        """Identify several URLs, reporting per-URL failures rather than failing all."""
        out: list[Detection | AppError] = []
        for url in urls:
            try:
                out.append(self.detect(url))
            except AppError as exc:
                out.append(exc)
        return out


detector = UrlDetector()


def content_ref(detection: Detection) -> ContentRef:
    return ContentRef(
        platform=detection.platform,
        url=detection.normalized,
        content_id=detection.content_id,
        url_kind=detection.url_kind,
        media_kind=detection.media_kind,
        confidence=detection.confidence,
    )
