"""The adapter contract.

One class per platform, implementing one protocol. Nothing about a specific
platform may live outside its adapter -- that rule from §4 is enforced by
``tests/test_architecture.py``, which fails if the core imports a platform
name.

Two design decisions worth stating:

**Unsupported is a value.** Methods that a platform cannot support return
``None`` or raise :class:`Unsupported`, never a bare exception. A YouTube
adapter with no download path must be *easy* to write correctly, because the
naive version -- returning an empty list and letting the UI treat it as
failure -- is what produces the "this site is broken" experience.

**Static capabilities, dynamic refinement.** Capabilities are declared as a
class attribute so ``GET /platforms`` needs no network calls, then refined per
URL by :meth:`refine_capabilities` where the platform allows it (a Vimeo video
is downloadable only for its owner).
"""
from __future__ import annotations

import re
from typing import ClassVar

from umf.core.contracts import Adapter, IdentifyResult
from umf.core.models import (  # noqa: F401 - re-exported for adapter authors
    Capabilities,
    ContentRef,
    DownloadOption,
    MediaKind,
    Metadata,
    PlatformCategory,
    SubtitleTrack,
    UrlKind,
)


class DeclarativeAdapter:
    """Base for adapters that are, at this phase, pure declaration.

    Most of the 21 platforms differ only in data: hosts, URL patterns, and an
    honest capability profile. Writing 21 copies of the same six methods would
    make the tree look modular while making it harder to change. This base
    carries the shared behaviour; each platform still has its own class and its
    own declaration, and any platform that grows real logic stops using this.
    """

    slug: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    category: ClassVar[PlatformCategory] = PlatformCategory.VIDEO_HOSTING
    hosts: ClassVar[tuple[str, ...]] = ()
    #: Regexes matched against the path (and query, where useful).
    url_patterns: ClassVar[tuple[str, ...]] = ()
    #: Used when the platform identifies content by URL path but the captured
    #: value is not a stable identifier.
    capabilities: ClassVar[Capabilities] = Capabilities()
    #: Shown in the UI next to a refusal, explaining the platform's rule.
    limitation_note: ClassVar[str] = ""

    _compiled: ClassVar[tuple[re.Pattern[str], ...]] = ()

    @classmethod
    def compiled_patterns(cls) -> tuple[re.Pattern[str], ...]:
        if not cls._compiled:
            cls._compiled = tuple(re.compile(pattern, re.IGNORECASE) for pattern in cls.url_patterns)
        return cls._compiled

    def host_matches(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def identify_url(self, url: str) -> IdentifyResult | None:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if not self.host_matches(parts.hostname or ""):
            return None
        target = parts.path + (("?" + parts.query) if parts.query else "")
        patterns = self.compiled_patterns()
        for pattern in patterns:
            match = pattern.search(target)
            if match:
                groups = match.groupdict()
                content_id = groups.get("id") or (match.group(1) if match.groups() else None)
                return IdentifyResult(content_id=content_id)
        # A host match with no pattern match is not an identification. This
        # matters most for PeerTube, which claims every host; returning a match
        # here let it shadow every adapter that came after it in the registry.
        return None if patterns else IdentifyResult(content_id=None)

    def refine_capabilities(self, ref: ContentRef) -> Capabilities:
        return type(self).capabilities

    def describe(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.display_name,
            "category": self.category.value,
            "hosts": list(self.hosts),
            "capabilities": self.capabilities.as_dict(),
        }


# ------------------------------------------------------------------ helpers
def normalise_id(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().strip("/")
    return value or None


__all__ = [
    "Adapter",
    "Capabilities",
    "ContentRef",
    "DeclarativeAdapter",
    "DownloadOption",
    "IdentifyResult",
    "Metadata",
    "SubtitleTrack",
    "normalise_id",
]
