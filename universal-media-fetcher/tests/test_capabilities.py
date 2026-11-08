"""Capability profiles must be honest.

These tests protect the product's central claim: that a refusal is explained
rather than discovered at download time. Each one encodes a fact verified
against the platform's own documentation, so a future edit that quietly turns a
``False`` into a ``True`` fails here rather than in production.
"""
from __future__ import annotations

import pytest

from umf.core.capabilities import engine
from umf.core.capabilities import engine as capability_engine
from umf.core.detector import detector
from umf.core.registry import registry
from umf.errors import AppError, ErrorCode

OTT_SLUGS = {
    "netflix", "prime_video", "jiohotstar", "sony_liv", "zee5",
    "apple_tv", "hulu", "max", "paramount_plus",
}


def caps(slug: str):
    return registry.get(slug).capabilities


# ---------------------------------------------------------------- downloadable
@pytest.mark.parametrize("slug", ["internet_archive", "peertube", "direct_media", "hls", "dash"])
def test_download_is_claimed_only_where_it_is_real(slug: str) -> None:
    assert caps(slug).authorized_download is True


@pytest.mark.parametrize(
    "slug",
    ["youtube", "twitch", "instagram", "facebook", "x", "reddit", "tiktok", "pinterest", "dailymotion"],
)
def test_social_and_hosting_platforms_refuse_download(slug: str) -> None:
    """YouTube's ToS forbids downloading; the others expose no file endpoint."""
    assert caps(slug).authorized_download is False
    assert caps(slug).reasons.get("authorized_download") == ErrorCode.DOWNLOAD_NOT_PERMITTED.value


@pytest.mark.parametrize("slug", sorted(OTT_SLUGS))
def test_ott_never_claims_download(slug: str) -> None:
    capabilities = caps(slug)
    assert capabilities.authorized_download is False
    assert capabilities.drm_protected is True
    assert capabilities.official_playback is True
    assert capabilities.reasons.get("authorized_download") == ErrorCode.DRM_PROTECTED.value


# ------------------------------------------------------------------- DRM rules
def test_ott_is_reported_as_drm_protected_not_invalid() -> None:
    """§5: DRM must never be reported as a broken URL."""
    detection = detector.detect("https://www.netflix.com/title/80100172")
    capabilities = engine.for_detection(detection)
    assert capabilities.drm_protected is True
    assert capabilities.reasons["authorized_download"] == "DRM_PROTECTED"


def test_requiring_download_on_netflix_raises_the_specific_code() -> None:
    detection = detector.detect("https://www.netflix.com/title/80100172")
    capabilities = engine.for_detection(detection)
    with pytest.raises(AppError) as caught:
        capability_engine.require(capabilities, "authorized_download")
    assert caught.value.code is ErrorCode.DRM_PROTECTED
    assert caught.value.http_status == 451
    # The user must be offered a legitimate alternative, not a dead end.
    assert caught.value.alternative


def test_requiring_download_on_youtube_raises_not_permitted() -> None:
    detection = detector.detect("https://youtu.be/dQw4w9WgXcQ")
    capabilities = engine.for_detection(detection)
    with pytest.raises(AppError) as caught:
        capability_engine.require(capabilities, "authorized_download")
    assert caught.value.code is ErrorCode.DOWNLOAD_NOT_PERMITTED
    assert caught.value.http_status == 403


def test_requiring_an_available_capability_does_not_raise() -> None:
    detection = detector.detect("https://archive.org/details/some_item")
    capability_engine.require(engine.for_detection(detection), "authorized_download")


# ------------------------------------------------------------------ summaries
def test_reasons_are_dropped_for_capabilities_that_are_available() -> None:
    """A stale reason next to a `true` would make the UI contradict itself."""
    for adapter in registry.adapters:
        capabilities = adapter.capabilities
        for name, code in capabilities.reasons.items():
            if getattr(capabilities, name, False):
                pytest.fail(f"{adapter.slug} claims {name}=True but still lists reason {code}")


def test_summary_partitions_capabilities() -> None:
    detection = detector.detect("https://youtu.be/dQw4w9WgXcQ")
    summary = engine.summary(engine.for_detection(detection))
    assert "metadata" in summary["available"]
    assert "authorized_download" in [entry["capability"] for entry in summary["unavailable"]]


def test_every_unavailable_capability_has_a_code() -> None:
    for adapter in registry.adapters:
        summary = engine.summary(adapter.capabilities)
        for entry in summary["unavailable"]:
            app_error = ErrorCode(entry["code"])  # raises if the code is unknown
            assert app_error.value == entry["code"]


# ------------------------------------------------------------------- platform
def test_authentication_required_matches_the_handoff_rules() -> None:
    for slug in ("instagram", "facebook", "x", "tiktok"):
        assert caps(slug).authentication_required is True
    for slug in ("youtube", "internet_archive", "peertube", "reddit"):
        assert caps(slug).authentication_required is False


def test_limitation_note_exists_wherever_download_is_refused() -> None:
    """§2: an unsupported operation must be explained, not just denied."""
    for adapter in registry.adapters:
        if not adapter.capabilities.authorized_download:
            assert adapter.capabilities.notes, f"{adapter.slug} refuses download without a reason"
