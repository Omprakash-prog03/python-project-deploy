"""OTT and streaming platforms.

All nine share one profile, and the reason is important enough to restate here:
**none of them publishes a content API**. Netflix closed its public API in 2014
and never reopened it; the other eight never had one. Their SDKs are partner
integrations for their own apps, not third-party content endpoints.

So the blocker is not DRM -- it is the absence of any supported way to ask
"what is this URL". §2 asks that DRM not be treated as a broken URL, and this
module honours that in the strongest available form: a Netflix link is
identified as Netflix, its content id is extracted, an official deep link is
produced, and the reason given is ``DRM_PROTECTED`` with an honest explanation
-- never ``INVALID_URL``.

Title and poster come from a licensed metadata aggregator (TMDB, Phase 3),
not from the platform. That is the authorised route and the only one.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from umf.adapters.base import DeclarativeAdapter, IdentifyResult
from umf.core.models import Capabilities, PlatformCategory, UrlKind

_OTT_RULE = (
    "This platform publishes no public content API, so a third-party service "
    "cannot read its catalogue. Offline downloads exist only inside the "
    "provider's own app, tied to its DRM."
)

OTT_CAPABILITIES = Capabilities(
    metadata=False,          # elevated to True in Phase 3 via TMDB
    preview=False,
    official_playback=True,
    authorized_download=False,
    subtitle_access=False,
    authentication_required=True,
    drm_protected=True,
    reasons={
        "metadata": "DRM_PROTECTED",
        "authorized_download": "DRM_PROTECTED",
    },
    notes=(_OTT_RULE,),
)


class OttAdapter(DeclarativeAdapter):
    """Shared behaviour for streaming services."""

    category = PlatformCategory.OTT
    capabilities = OTT_CAPABILITIES
    limitation_note = _OTT_RULE
    #: Host used to build the official deep link.
    official_host: str = ""

    def official_url(self, content_id: str | None) -> str | None:
        if not content_id or not self.official_host:
            return None
        return f"https://{self.official_host}/{content_id}"

    def identify_url(self, url: str) -> IdentifyResult | None:
        result = super().identify_url(url)
        if result is None:
            return None
        parts = urlsplit(url)
        link = self.official_url(result.content_id) or f"{parts.scheme}://{parts.netloc}"
        return IdentifyResult(
            content_id=result.content_id,
            url_kind=UrlKind.SHARING,
            confidence=result.confidence,
            note=f"Official page: {link}",
        )


class NetflixAdapter(OttAdapter):
    slug = "netflix"
    display_name = "Netflix"
    hosts = ("netflix.com", "www.netflix.com")
    url_patterns = (r"/(?:title|watch|browse)/(?P<id>\d+)",)
    official_host = "www.netflix.com/title"


class PrimeVideoAdapter(OttAdapter):
    slug = "prime_video"
    display_name = "Amazon Prime Video"
    hosts = ("primevideo.com", "amazon.com", "www.amazon.com", "www.primevideo.com")
    url_patterns = (r"/detail/(?P<id>[A-Za-z0-9]+)", r"/gp/video/detail/(?P<id>[A-Za-z0-9]+)")
    official_host = "www.primevideo.com/detail"


class JioHotstarAdapter(OttAdapter):
    slug = "jiohotstar"
    display_name = "JioHotstar"
    hosts = ("hotstar.com", "jiohotstar.com", "www.hotstar.com", "www.jiohotstar.com")
    url_patterns = (r"/(?:in/)?(?:movies|shows|sports)/(?P<id>[^/]+/\d+)", r"/watch/(?P<id>\d+)")
    official_host = "www.jiohotstar.com"


class SonyLivAdapter(OttAdapter):
    slug = "sony_liv"
    display_name = "Sony LIV"
    hosts = ("sonyliv.com", "www.sonyliv.com")
    url_patterns = (r"/(?:detail|watch)/(?P<id>[^/?#]+)",)
    official_host = "www.sonyliv.com"


class Zee5Adapter(OttAdapter):
    slug = "zee5"
    display_name = "ZEE5"
    hosts = ("zee5.com", "www.zee5.com")
    url_patterns = (r"/(?:movies|tvshows|web-series)/(?P<id>[^?#]*\d+)", r"/watch/(?P<id>[^/?#]+)")
    official_host = "www.zee5.com"


class AppleTvAdapter(OttAdapter):
    slug = "apple_tv"
    display_name = "Apple TV+"
    hosts = ("tv.apple.com", "apple.com")
    url_patterns = (r"/(?:[a-z]{2}/)?(?:movie|show|episode)/(?P<id>[^/]+/[A-Za-z0-9._-]+)",)
    official_host = "tv.apple.com"


class HuluAdapter(OttAdapter):
    slug = "hulu"
    display_name = "Hulu"
    hosts = ("hulu.com", "www.hulu.com")
    url_patterns = (r"/watch/(?P<id>[A-Za-z0-9-]+)", r"/series/(?P<id>[A-Za-z0-9-]+)")
    official_host = "www.hulu.com/watch"


class MaxAdapter(OttAdapter):
    slug = "max"
    display_name = "Max"
    hosts = ("max.com", "play.max.com", "www.max.com")
    url_patterns = (r"/(?:movie|show|movies|shows|video/watch)/(?P<id>[A-Za-z0-9._-]+)",)
    official_host = "play.max.com"


class ParamountAdapter(OttAdapter):
    slug = "paramount_plus"
    display_name = "Paramount+"
    hosts = ("paramountplus.com", "www.paramountplus.com")
    url_patterns = (
        r"/(?:shows|movies)/video/(?P<id>[A-Za-z0-9]+)",
        r"/(?:shows|movies)/(?P<id>[A-Za-z0-9-]+)",
    )
    official_host = "www.paramountplus.com"


OTT_ADAPTERS = (
    NetflixAdapter,
    PrimeVideoAdapter,
    JioHotstarAdapter,
    SonyLivAdapter,
    Zee5Adapter,
    AppleTvAdapter,
    HuluAdapter,
    MaxAdapter,
    ParamountAdapter,
)
