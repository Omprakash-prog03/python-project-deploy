"""The adapter contract, defined in core.

Direction of dependency matters: core defines what an adapter must be, and
adapters implement it. If the protocol lived in ``umf.adapters`` instead, every
core module would import the adapter package, and the boundary §4 asks for
would exist only in the directory listing.

``tests/test_architecture.py`` enforces this: core may not import
``umf.adapters`` anywhere except the registry, which is the one place allowed to
assemble the integrations.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from umf.core.models import (
    Capabilities,
    ContentRef,
    MediaKind,
    PlatformCategory,
    UrlKind,
)


@dataclass(frozen=True, slots=True)
class IdentifyResult:
    """What an adapter can say about a URL without any network access."""

    content_id: str | None
    url_kind: UrlKind = UrlKind.SHARING
    media_kind: MediaKind = MediaKind.UNKNOWN
    confidence: float = 1.0
    note: str = ""


@runtime_checkable
class Adapter(Protocol):
    """Every platform integration implements this.

    Phase 0 uses the first four members; the async methods arrive with the
    metadata and download phases, and are declared in the architecture document
    rather than stubbed here -- a stub returning ``None`` would be the kind of
    fake success §16 forbids.
    """

    slug: str
    display_name: str
    category: PlatformCategory
    hosts: tuple[str, ...]
    capabilities: Capabilities

    def identify_url(self, url: str) -> IdentifyResult | None: ...

    def refine_capabilities(self, ref: ContentRef) -> Capabilities: ...


__all__ = ["Adapter", "IdentifyResult"]
