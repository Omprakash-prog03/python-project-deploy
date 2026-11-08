"""Social platforms.

Every platform in this group is metadata-only, and for one shared reason: their
APIs serve accounts that have authorised your application, not arbitrary public
URLs. Resolving a stranger's Instagram post would require scraping behind a
login wall, which §11 rules out.

The profiles record that honestly, including which platforms will work
immediately (Reddit, Pinterest: public metadata) and which return
``AUTHENTICATION_REQUIRED`` until the user connects an account.
"""
from __future__ import annotations

from umf.adapters.base import DeclarativeAdapter
from umf.core.models import Capabilities, PlatformCategory

_INSTAGRAM_RULE = (
    "Meta deprecated the Basic Display API on 4 December 2024. The Graph API "
    "serves only Business or Creator accounts that authorised this app, after "
    "app review, so public post URLs cannot be resolved."
)
_FACEBOOK_RULE = (
    "Facebook video metadata is available through the Graph API only for Pages "
    "and accounts that authorised this app."
)
_X_RULE = (
    "X's API is metered and paid. Metadata is reachable on a paid tier; media "
    "download is not offered by the API."
)
_REDDIT_RULE = (
    "Reddit's public JSON exposes post metadata and the outbound media link. "
    "Reddit itself does not host most video, so the link is passed through."
)
_TIKTOK_RULE = (
    "TikTok's Display API requires application approval and covers accounts "
    "that authorised the app; public video URLs are not resolvable."
)
_PINTEREST_RULE = "Pinterest pins resolve to metadata and an outbound link; the pin itself is not a video file."


class InstagramAdapter(DeclarativeAdapter):
    slug = "instagram"
    display_name = "Instagram"
    category = PlatformCategory.SOCIAL
    hosts = ("instagram.com", "instagr.am")
    url_patterns = (
        r"/(?:p|reel|reels|tv)/(?P<id>[A-Za-z0-9_-]+)",
        r"/stories/(?P<id>[A-Za-z0-9_.]+/\d+)",
    )
    limitation_note = _INSTAGRAM_RULE
    capabilities = Capabilities(
        metadata=False,
        preview=False,
        official_playback=True,
        authorized_download=False,
        subtitle_access=False,
        authentication_required=True,
        drm_protected=False,
        reasons={
            "metadata": "AUTHENTICATION_REQUIRED",
            "authorized_download": "DOWNLOAD_NOT_PERMITTED",
        },
        notes=(_INSTAGRAM_RULE,),
    )


class FacebookAdapter(DeclarativeAdapter):
    slug = "facebook"
    display_name = "Facebook"
    category = PlatformCategory.SOCIAL
    hosts = ("facebook.com", "fb.watch", "m.facebook.com", "web.facebook.com")
    url_patterns = (
        r"/videos/(?P<id>\d+)",
        r"/watch/?\?(?:.*&)?v=(?P<id>\d+)",
        r"/reel/(?P<id>\d+)",
        r"^/(?P<id>[A-Za-z0-9]+)(?:$|[/?#])",
    )
    limitation_note = _FACEBOOK_RULE
    capabilities = Capabilities(
        metadata=False,
        preview=False,
        official_playback=True,
        authorized_download=False,
        authentication_required=True,
        reasons={"metadata": "AUTHENTICATION_REQUIRED", "authorized_download": "DOWNLOAD_NOT_PERMITTED"},
        notes=(_FACEBOOK_RULE,),
    )


class TwitterAdapter(DeclarativeAdapter):
    slug = "x"
    display_name = "X (Twitter)"
    category = PlatformCategory.SOCIAL
    hosts = ("twitter.com", "x.com", "mobile.twitter.com", "t.co")
    url_patterns = (r"/(?P<user>[A-Za-z0-9_]+)/status/(?P<id>\d+)",)
    limitation_note = _X_RULE
    capabilities = Capabilities(
        metadata=False,
        preview=False,
        official_playback=True,
        authorized_download=False,
        authentication_required=True,
        reasons={"metadata": "AUTHENTICATION_REQUIRED", "authorized_download": "DOWNLOAD_NOT_PERMITTED"},
        notes=(_X_RULE,),
    )


class RedditAdapter(DeclarativeAdapter):
    slug = "reddit"
    display_name = "Reddit"
    category = PlatformCategory.SOCIAL
    hosts = ("reddit.com", "old.reddit.com", "www.reddit.com", "redd.it", "v.redd.it")
    url_patterns = (r"/r/[^/]+/comments/(?P<id>[a-z0-9]+)", r"^/(?P<id>[a-z0-9]{5,8})(?:$|[/?#])")
    limitation_note = _REDDIT_RULE
    capabilities = Capabilities(
        metadata=True,
        preview=True,
        official_playback=True,
        authorized_download=False,
        subtitle_access=False,
        authentication_required=False,
        reasons={"authorized_download": "DOWNLOAD_NOT_PERMITTED"},
        notes=(_REDDIT_RULE,),
    )


class TikTokAdapter(DeclarativeAdapter):
    slug = "tiktok"
    display_name = "TikTok"
    category = PlatformCategory.SOCIAL
    hosts = ("tiktok.com", "vm.tiktok.com", "m.tiktok.com")
    url_patterns = (r"/@[^/]+/video/(?P<id>\d+)", r"^/(?P<id>[A-Za-z0-9]{6,})(?:$|[/?#])")
    limitation_note = _TIKTOK_RULE
    capabilities = Capabilities(
        metadata=False,
        preview=False,
        official_playback=True,
        authorized_download=False,
        authentication_required=True,
        reasons={"metadata": "AUTHENTICATION_REQUIRED", "authorized_download": "DOWNLOAD_NOT_PERMITTED"},
        notes=(_TIKTOK_RULE,),
    )


class PinterestAdapter(DeclarativeAdapter):
    slug = "pinterest"
    display_name = "Pinterest"
    category = PlatformCategory.SOCIAL
    hosts = ("pinterest.com", "pin.it", "www.pinterest.com")
    url_patterns = (r"/pin/(?P<id>\d+)", r"^/(?P<id>[A-Za-z0-9]{4,})(?:$|[/?#])")
    limitation_note = _PINTEREST_RULE
    capabilities = Capabilities(
        metadata=True,
        preview=True,
        official_playback=False,
        authorized_download=False,
        authentication_required=False,
        reasons={"authorized_download": "DOWNLOAD_NOT_PERMITTED"},
        notes=(_PINTEREST_RULE,),
    )


SOCIAL_ADAPTERS = (
    InstagramAdapter,
    FacebookAdapter,
    TwitterAdapter,
    RedditAdapter,
    TikTokAdapter,
    PinterestAdapter,
)
