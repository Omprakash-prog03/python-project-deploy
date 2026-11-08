"""Direct media: progressive files and adaptive manifests.

This is the one category where download is unconditional, which is why it is
built first. Nothing here needs an API key, a login or a permission -- the file
is simply at the URL.

Extensions are matched from the path only. A query string may say
``?file=video.mp4`` and a CDN may not use extensions at all, so a miss here is
not an error: the capability engine reports ``UNKNOWN`` and the Phase-1 fetcher
sniffs the content type before committing to a download.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from umf.adapters.base import DeclarativeAdapter, IdentifyResult
from umf.core.models import Capabilities, MediaKind, PlatformCategory, UrlKind

#: Progressive containers a browser or FFmpeg can stream to disk.
VIDEO_EXTENSIONS = {
    ".mp4": "mp4",
    ".webm": "webm",
    ".mov": "mov",
    ".mkv": "mkv",
    ".m4v": "mp4",
    ".avi": "avi",
}
AUDIO_EXTENSIONS = {".mp3": "mp3", ".m4a": "m4a", ".aac": "aac", ".opus": "opus", ".flac": "flac"}
PLAYLIST_EXTENSIONS = {".m3u8": "hls"}
MANIFEST_EXTENSIONS = {".mpd": "dash"}


def classify_path(path: str) -> tuple[MediaKind, str | None, UrlKind]:
    """Infer the media kind and container from a URL path."""
    lowered = path.lower()
    for extension, container in PLAYLIST_EXTENSIONS.items():
        if lowered.endswith(extension):
            return MediaKind.PLAYLIST, container, UrlKind.DIRECT
    for extension, container in MANIFEST_EXTENSIONS.items():
        if lowered.endswith(extension):
            return MediaKind.MANIFEST, container, UrlKind.DIRECT
    for extension, container in VIDEO_EXTENSIONS.items():
        if lowered.endswith(extension):
            return MediaKind.VIDEO, container, UrlKind.DIRECT
    for extension, container in AUDIO_EXTENSIONS.items():
        if lowered.endswith(extension):
            return MediaKind.AUDIO, container, UrlKind.DIRECT
    return MediaKind.UNKNOWN, None, UrlKind.UNKNOWN


class DirectMediaAdapter(DeclarativeAdapter):
    """Handles media that is served as a file, from any host."""

    slug = "direct_media"
    display_name = "Direct media"
    category = PlatformCategory.DIRECT_MEDIA
    hosts = ()  # matches any host; selected by extension instead

    capabilities = Capabilities(
        metadata=True,
        preview=True,
        official_playback=True,
        authorized_download=True,
        subtitle_access=False,
        authentication_required=False,
        drm_protected=False,
        notes=(
            "Direct files and adaptive manifests can be downloaded because the "
            "server is already publishing them; no platform permission is involved.",
        ),
    )

    def identify_url(self, url: str) -> IdentifyResult | None:
        parts = urlsplit(url)
        media_kind, container, url_kind = classify_path(parts.path)
        if media_kind is MediaKind.UNKNOWN:
            return None
        return IdentifyResult(
            content_id=None,
            url_kind=url_kind,
            media_kind=media_kind,
            confidence=0.8,  # extension implies the type; content sniffing confirms
            note=f"{container} container inferred from the URL path",
        )


class HlsAdapter(DirectMediaAdapter):
    """Kept separate so HLS-specific handling (segment fetch, remux) has a home."""

    slug = "hls"
    display_name = "HLS stream"
    capabilities = Capabilities(
        metadata=False,
        preview=True,
        official_playback=True,
        authorized_download=True,
        subtitle_access=True,  # HLS carries WebVTT tracks
        authentication_required=False,
        drm_protected=False,
        notes=(
            "Segments are fetched and remuxed with FFmpeg. Encrypted HLS (SAMPLE-AES) "
            "is refused as DRM_PROTECTED rather than attempted.",
        ),
    )

    def identify_url(self, url: str) -> IdentifyResult | None:
        if not urlsplit(url).path.lower().endswith(".m3u8"):
            return None
        return IdentifyResult(None, UrlKind.DIRECT, MediaKind.PLAYLIST, 0.9, "HLS manifest")


class DashAdapter(DirectMediaAdapter):
    slug = "dash"
    display_name = "MPEG-DASH stream"
    capabilities = Capabilities(
        metadata=False,
        preview=True,
        official_playback=True,
        authorized_download=True,
        subtitle_access=True,
        authentication_required=False,
        drm_protected=False,
        notes=(
            "Clear DASH is remuxed like HLS. A manifest carrying ContentProtection "
            "is refused as DRM_PROTECTED.",
        ),
    )

    def identify_url(self, url: str) -> IdentifyResult | None:
        if not urlsplit(url).path.lower().endswith(".mpd"):
            return None
        return IdentifyResult(None, UrlKind.DIRECT, MediaKind.MANIFEST, 0.9, "DASH manifest")
