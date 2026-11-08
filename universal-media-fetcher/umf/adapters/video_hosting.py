"""Video hosting platforms.

The capability profiles here are the point of this module. They encode, per
platform, what the platform actually authorises -- verified against its API
documentation and terms, not assumed from "a downloader should be able to do
this".

Two of the six can download. The other four cannot, and say so with a reason
rather than failing at retrieval time.
"""
from __future__ import annotations

from umf.adapters.base import DeclarativeAdapter
from umf.core.models import Capabilities, PlatformCategory

_YOUTUBE_RULE = (
    "YouTube's Terms of Service prohibit downloading content unless YouTube "
    "displays a download link for it, and the Data API v3 has no download "
    "endpoint. Metadata and captions are available; files are not."
)
_VIMEO_RULE = (
    "Vimeo returns download URLs only when the API token belongs to the video's "
    "owner and the app was created by that owner. Public videos are metadata-only."
)
_TWITCH_RULE = (
    "Twitch VODs and clips are available through Helix for metadata and embedded "
    "playback; the API exposes no downloadable file."
)
_DM_RULE = (
    "Some Dailymotion uploads publish direct progressive URLs; many do not. "
    "Download is offered only when the API returns a usable file."
)


class YouTubeAdapter(DeclarativeAdapter):
    slug = "youtube"
    display_name = "YouTube"
    category = PlatformCategory.VIDEO_HOSTING
    hosts = ("youtube.com", "youtu.be", "youtube-nocookie.com", "m.youtube.com")
    url_patterns = (
        r"/watch\?(?:.*&)?v=(?P<id>[A-Za-z0-9_-]{11})",
        r"^/(?P<id>[A-Za-z0-9_-]{11})(?:$|[?&])",
        r"/(?:shorts|embed|live|v)/(?P<id>[A-Za-z0-9_-]{11})",
    )
    limitation_note = _YOUTUBE_RULE
    capabilities = Capabilities(
        metadata=True,
        preview=True,
        official_playback=True,
        authorized_download=False,
        # captions.download exists but only for videos the caller owns, so for
        # an arbitrary public URL there is no caption to retrieve.
        subtitle_access=False,
        authentication_required=False,
        drm_protected=False,
        reasons={
            "authorized_download": "DOWNLOAD_NOT_PERMITTED",
            "subtitle_access": "AUTHENTICATION_REQUIRED",
        },
        notes=(_YOUTUBE_RULE,),
    )


class VimeoAdapter(DeclarativeAdapter):
    slug = "vimeo"
    display_name = "Vimeo"
    category = PlatformCategory.VIDEO_HOSTING
    hosts = ("vimeo.com", "player.vimeo.com")
    url_patterns = (
        r"^/(?:video/)?(?P<id>\d{6,12})(?:$|[/?#])",
    )
    limitation_note = _VIMEO_RULE
    capabilities = Capabilities(
        metadata=True,
        preview=True,
        official_playback=True,
        authorized_download=False,   # refined to True when the caller owns it
        subtitle_access=True,
        authentication_required=False,
        drm_protected=False,
        reasons={"authorized_download": "AUTHENTICATION_REQUIRED"},
        notes=(_VIMEO_RULE,),
    )

    def refine_capabilities(self, ref):
        """Owner tokens unlock the ``download`` key; nothing else does."""
        from dataclasses import replace

        if getattr(ref, "content_id", None) and getattr(ref, "confidence", 1.0) >= 1.0:
            # Ownership is unknown at identify time; keep the refusal until an
            # authenticated PlatformConnection proves otherwise.
            return self.capabilities
        return replace(self.capabilities)


class DailymotionAdapter(DeclarativeAdapter):
    slug = "dailymotion"
    display_name = "Dailymotion"
    category = PlatformCategory.VIDEO_HOSTING
    hosts = ("dailymotion.com", "dai.ly")
    url_patterns = (r"/video/(?P<id>[A-Za-z0-9]+)", r"^/(?P<id>[A-Za-z0-9]{6,})(?:$|[/?#])")
    limitation_note = _DM_RULE
    capabilities = Capabilities(
        metadata=True,
        preview=True,
        official_playback=True,
        authorized_download=False,
        subtitle_access=True,
        authentication_required=False,
        drm_protected=False,
        reasons={"authorized_download": "DOWNLOAD_NOT_PERMITTED"},
        notes=(_DM_RULE,),
    )


class TwitchAdapter(DeclarativeAdapter):
    slug = "twitch"
    display_name = "Twitch"
    category = PlatformCategory.VIDEO_HOSTING
    hosts = ("twitch.tv", "clips.twitch.tv", "m.twitch.tv")
    url_patterns = (
        r"/videos/(?P<id>\d+)",
        r"/clip/(?P<id>[A-Za-z0-9_-]+)",
        r"^/(?P<id>[A-Za-z0-9_]+)/clip/(?P<clip>[A-Za-z0-9_-]+)",
    )

    def identify_url(self, url: str):
        """clips.twitch.tv puts the clip id directly in the path."""
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if (parts.hostname or "").lower().endswith("clips.twitch.tv"):
            clip_id = parts.path.strip("/")
            if clip_id:
                from umf.adapters.base import IdentifyResult

                return IdentifyResult(content_id=clip_id)
        return super().identify_url(url)
    limitation_note = _TWITCH_RULE
    capabilities = Capabilities(
        metadata=True,
        preview=True,
        official_playback=True,
        authorized_download=False,
        subtitle_access=False,
        authentication_required=False,
        drm_protected=False,
        reasons={"authorized_download": "DOWNLOAD_NOT_PERMITTED"},
        notes=(_TWITCH_RULE,),
    )


class InternetArchiveAdapter(DeclarativeAdapter):
    """The flagship integration: public metadata, explicit downloads."""

    slug = "internet_archive"
    display_name = "Internet Archive"
    category = PlatformCategory.VIDEO_HOSTING
    hosts = ("archive.org", "www.archive.org")
    url_patterns = (
        r"/details/(?P<id>[^/?#]+)",
        r"/download/(?P<id>[^/?#]+)",
        r"/embed/(?P<id>[^/?#]+)",
    )
    capabilities = Capabilities(
        metadata=True,
        preview=True,
        official_playback=True,
        authorized_download=True,
        subtitle_access=True,
        authentication_required=False,
        drm_protected=False,
        notes=("Items publish an explicit file list with real byte counts and allow download.",),
    )


class PeerTubeAdapter(DeclarativeAdapter):
    """PeerTube is federated, so it cannot be identified by host alone.

    Identification falls back to URL shape, which is a weaker signal -- any site
    with ``/videos/watch/<uuid>`` matches. The confidence is lowered to say so,
    and Phase 2 confirms against the instance's ``/api/v1/videos/{id}`` before
    treating the match as real.
    """

    slug = "peertube"
    display_name = "PeerTube"
    category = PlatformCategory.VIDEO_HOSTING
    hosts = ()
    url_patterns = (
        r"/videos/watch/(?P<id>[0-9a-fA-F-]{36})",
        r"/w/(?P<id>[A-Za-z0-9]{20,24})",
        r"/videos/embed/(?P<id>[0-9a-fA-F-]{36})",
    )
    capabilities = Capabilities(
        metadata=True,
        preview=True,
        official_playback=True,
        authorized_download=True,
        subtitle_access=True,
        authentication_required=False,
        drm_protected=False,
        notes=(
            "Instance-dependent: some disable download in their configuration. "
            "Resolved per instance at metadata time.",
        ),
    )

    def host_matches(self, host: str) -> bool:
        return True

    def identify_url(self, url):
        result = super().identify_url(url)
        if result is None:
            return None
        from dataclasses import replace

        # Host-based confirmation is impossible for a federated platform.
        return replace(result, confidence=0.6, note="federated host; verified at metadata time")

    def refine_capabilities(self, ref):
        """A shape-only match cannot promise capabilities the instance may disable."""
        from dataclasses import replace

        if getattr(ref, "confidence", 1.0) < 0.7:
            return replace(
                self.capabilities,
                notes=(
                    *self.capabilities.notes,
                    "Federated host: capabilities are confirmed once the instance responds.",
                ),
            )
        return self.capabilities


VIDEO_HOSTING_ADAPTERS = (
    YouTubeAdapter,
    VimeoAdapter,
    DailymotionAdapter,
    TwitchAdapter,
    InternetArchiveAdapter,
    PeerTubeAdapter,
)
