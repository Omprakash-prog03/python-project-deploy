"""Adapter registry and platform resolution.

Ordering matters. Host-specific adapters are consulted before pattern-only
ones, because PeerTube claims every host by URL shape and would otherwise
shadow YouTube. Direct media is consulted last, since any file extension check
loses to a real platform match.
"""
from __future__ import annotations

from umf.adapters.base import Adapter
from umf.adapters.direct_media import DashAdapter, DirectMediaAdapter, HlsAdapter
from umf.adapters.ott import OTT_ADAPTERS
from umf.adapters.social import SOCIAL_ADAPTERS
from umf.adapters.video_hosting import VIDEO_HOSTING_ADAPTERS


class AdapterRegistry:
    def __init__(self, adapters: list[Adapter]) -> None:
        self._adapters = adapters
        self._by_slug = {adapter.slug: adapter for adapter in adapters}

    @property
    def adapters(self) -> list[Adapter]:
        return list(self._adapters)

    def get(self, slug: str) -> Adapter | None:
        return self._by_slug.get(slug)

    def __len__(self) -> int:
        return len(self._adapters)

    def match(self, url: str) -> tuple[Adapter, object] | None:
        """Return the first adapter that claims this URL, with its result."""
        for adapter in self._adapters:
            result = adapter.identify_url(url)
            if result is not None:
                return adapter, result
        return None


def build_default_registry() -> AdapterRegistry:
    """Assemble every integration.

    Explicit ordering rather than alphabetical: host-matched adapters first,
    shape-matched (PeerTube) next, direct media last.
    """
    ordered: list[Adapter] = []
    for cls in VIDEO_HOSTING_ADAPTERS + SOCIAL_ADAPTERS + OTT_ADAPTERS:
        ordered.append(cls())
    ordered.append(HlsAdapter())
    ordered.append(DashAdapter())
    ordered.append(DirectMediaAdapter())
    return AdapterRegistry(ordered)


registry = build_default_registry()
