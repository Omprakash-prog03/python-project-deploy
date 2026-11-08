"""URL detection across every integrated platform.

The table is the specification: one representative URL per platform, with the
identifier that must come back. A platform added without a row here is a
platform added without a test, which §15 forbids.
"""
from __future__ import annotations

import pytest

from umf.core.detector import detector
from umf.core.models import MediaKind, UrlKind
from umf.core.registry import registry
from umf.errors import AppError, ErrorCode

# (url, expected platform, expected content id)
PLATFORM_URLS = [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/abc123XYZ_-", "youtube", "abc123XYZ_-"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://vimeo.com/76979871", "vimeo", "76979871"),
    ("https://player.vimeo.com/video/76979871", "vimeo", "76979871"),
    ("https://www.dailymotion.com/video/x7tgad0", "dailymotion", "x7tgad0"),
    ("https://dai.ly/x7tgad0", "dailymotion", "x7tgad0"),
    ("https://www.twitch.tv/videos/1234567890", "twitch", "1234567890"),
    ("https://clips.twitch.tv/AwkwardHelplessSalamanderSwiftRage", "twitch", "AwkwardHelplessSalamanderSwiftRage"),
    ("https://archive.org/details/night_of_the_living_dead", "internet_archive", "night_of_the_living_dead"),
    (
        "https://peertube.example.org/videos/watch/9f4e2b1a-0c3d-4e5f-8a9b-1c2d3e4f5a6b",
        "peertube",
        "9f4e2b1a-0c3d-4e5f-8a9b-1c2d3e4f5a6b",
    ),
    ("https://www.instagram.com/reel/CxYz123AbCd/", "instagram", "CxYz123AbCd"),
    ("https://www.instagram.com/p/CxYz123AbCd/", "instagram", "CxYz123AbCd"),
    ("https://www.facebook.com/videos/1234567890123456/", "facebook", "1234567890123456"),
    ("https://web.facebook.com/watch?v=1234567890", "facebook", "1234567890"),
    ("https://x.com/someuser/status/1234567890123456789", "x", "1234567890123456789"),
    ("https://twitter.com/someuser/status/1234567890", "x", "1234567890"),
    ("https://www.reddit.com/r/videos/comments/abc123/title_here/", "reddit", "abc123"),
    ("https://www.tiktok.com/@user/video/7212345678901234567", "tiktok", "7212345678901234567"),
    ("https://www.pinterest.com/pin/123456789012345678/", "pinterest", "123456789012345678"),
    ("https://www.netflix.com/title/80100172", "netflix", "80100172"),
    ("https://www.netflix.com/watch/80100172", "netflix", "80100172"),
    ("https://www.primevideo.com/detail/0ABCDEF123456", "prime_video", "0ABCDEF123456"),
    ("https://www.jiohotstar.com/in/movies/abc/12345", "jiohotstar", "abc/12345"),
    ("https://www.sonyliv.com/detail/show-name-1000123", "sony_liv", "show-name-1000123"),
    ("https://www.zee5.com/movies/movie-name/0-0-123456", "zee5", "movie-name/0-0-123456"),
    ("https://tv.apple.com/us/movie/some-movie/umc.cmc.1234abcd", "apple_tv", "some-movie/umc.cmc.1234abcd"),
    ("https://www.hulu.com/watch/abc-123-def", "hulu", "abc-123-def"),
    ("https://play.max.com/movie/abc-123", "max", "abc-123"),
    ("https://www.paramountplus.com/shows/video/AbCdEf123456", "paramount_plus", "AbCdEf123456"),
]


@pytest.mark.parametrize("url,platform,content_id", PLATFORM_URLS)
def test_platform_identification(url: str, platform: str, content_id: str) -> None:
    detection = detector.detect(url)
    assert detection.platform == platform
    assert detection.content_id == content_id


@pytest.mark.parametrize("url,platform,content_id", PLATFORM_URLS)
def test_no_adapter_is_shadowed(url: str, platform: str, content_id: str) -> None:
    """Regression: a host-agnostic adapter returning a match on every URL.

    PeerTube claims any host, so a bug where it reported a match without a
    pattern match silently swallowed Instagram, X, Reddit, every OTT adapter
    and all direct media. This asserts each platform is reachable.
    """
    match = registry.match(url)
    assert match is not None
    assert match[0].slug == platform


@pytest.mark.parametrize(
    "url,media_kind,platform",
    [
        ("https://cdn.example.com/videos/clip.mp4", MediaKind.VIDEO, "direct_media"),
        ("https://cdn.example.com/videos/clip.webm", MediaKind.VIDEO, "direct_media"),
        ("https://cdn.example.com/movies/film.mkv", MediaKind.VIDEO, "direct_media"),
        ("https://cdn.example.com/movies/film.mov", MediaKind.VIDEO, "direct_media"),
        ("https://cdn.example.com/live/index.m3u8", MediaKind.PLAYLIST, "hls"),
        ("https://cdn.example.com/vod/manifest.mpd", MediaKind.MANIFEST, "dash"),
    ],
)
def test_direct_media_classification(url: str, media_kind: MediaKind, platform: str) -> None:
    detection = detector.detect(url)
    assert detection.platform == platform
    assert detection.media_kind is media_kind
    assert detection.url_kind is UrlKind.DIRECT


@pytest.mark.parametrize(
    "url,code",
    [
        ("http://169.254.169.254/latest/meta-data/", ErrorCode.INVALID_URL),
        ("http://127.0.0.1/", ErrorCode.INVALID_URL),
        ("http://localhost/", ErrorCode.INVALID_URL),
        ("file:///etc/passwd", ErrorCode.INVALID_URL),
        ("not a url at all", ErrorCode.INVALID_URL),
        ("https://unknown-site.example/page", ErrorCode.UNSUPPORTED_PLATFORM),
    ],
)
def test_rejections(url: str, code: ErrorCode) -> None:
    with pytest.raises(AppError) as caught:
        detector.detect(url)
    assert caught.value.code is code


def test_unknown_platform_is_not_reported_as_invalid() -> None:
    """A working URL on an unintegrated site is unsupported, not invalid."""
    with pytest.raises(AppError) as caught:
        detector.detect("https://example.com/some/page")
    assert caught.value.code is ErrorCode.UNSUPPORTED_PLATFORM
    assert caught.value.user_message != "The supplied URL is invalid."


def test_peertube_confidence_reflects_the_weak_signal() -> None:
    detection = detector.detect(
        "https://random-instance.example/videos/watch/9f4e2b1a-0c3d-4e5f-8a9b-1c2d3e4f5a6b"
    )
    assert detection.platform == "peertube"
    assert detection.confidence < 1.0, "a shape-only match must not claim certainty"


def test_batch_reports_per_url_failures() -> None:
    results = detector.detect_many(
        ["https://youtu.be/dQw4w9WgXcQ", "http://127.0.0.1/", "https://example.com/x"]
    )
    assert not isinstance(results[0], AppError)
    assert isinstance(results[1], AppError) and results[1].code is ErrorCode.INVALID_URL
    assert isinstance(results[2], AppError) and results[2].code is ErrorCode.UNSUPPORTED_PLATFORM


def test_every_registered_adapter_has_a_platform_row() -> None:
    """§15: no adapter ships without a test row above."""
    covered = {platform for _url, platform, _id in PLATFORM_URLS}
    covered |= {"direct_media", "hls", "dash"}
    missing = {adapter.slug for adapter in registry.adapters} - covered
    assert not missing, f"adapters with no detection test: {sorted(missing)}"


def test_registry_contains_every_spec_platform() -> None:
    required = {
        "youtube", "vimeo", "dailymotion", "twitch", "internet_archive", "peertube",
        "instagram", "facebook", "x", "reddit", "tiktok", "pinterest",
        "netflix", "prime_video", "jiohotstar", "sony_liv", "zee5",
        "apple_tv", "hulu", "max", "paramount_plus",
        "direct_media",
    }
    assert required <= {adapter.slug for adapter in registry.adapters}
