"""REST API for Phase 0.

Two endpoints are live, and both are honest about what they do not yet do:

* ``GET  /api/v1/platforms`` -- the capability matrix, no network required.
* ``POST /api/v1/analyze``   -- identify a URL and report capabilities.

``/analyze`` deliberately returns ``metadata: null`` with a ``pending`` note.
Phase 0 performs no outbound requests, so the alternative -- returning a stub
title -- would be exactly the fabricated response §16 forbids.

The remaining endpoints from §13 are declared in ``PLANNED_ENDPOINTS`` so the
gap between designed and built is visible in the API itself rather than only in
the roadmap.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body
from pydantic import BaseModel, Field

from umf.core.capabilities import engine as capability_engine
from umf.core.detector import detector
from umf.core.registry import registry
from umf.errors import AppError

router = APIRouter(prefix="/api/v1", tags=["media"])

PLANNED_ENDPOINTS = [
    ("GET", "/api/v1/media/{id}", "Phase 2 -- needs adapters to return metadata"),
    ("GET", "/api/v1/media/{id}/capabilities", "Phase 2"),
    ("POST", "/api/v1/downloads", "Phase 1 -- needs the download manager"),
    ("GET", "/api/v1/downloads/{id}", "Phase 1"),
    ("POST", "/api/v1/downloads/{id}/cancel", "Phase 1"),
    ("GET", "/api/v1/history", "Phase 4 -- needs user accounts"),
]


class AnalyzeRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096, description="A sharing or direct media URL")
    urls: list[str] = Field(default_factory=list, max_length=20, description="Additional URLs")


class CapabilityOut(BaseModel):
    metadata: bool
    preview: bool
    official_playback: bool
    authorized_download: bool
    subtitle_access: bool
    authentication_required: bool
    drm_protected: bool
    reasons: dict[str, str] = {}
    notes: list[str] = []


class AnalyzeOut(BaseModel):
    platform: str
    platform_name: str
    content_id: str | None
    url: str
    normalized_url: str
    url_kind: str
    media_kind: str
    confidence: float
    note: str = ""
    capabilities: CapabilityOut
    capability_summary: dict[str, Any]
    metadata: None = None
    metadata_status: str = "pending"
    metadata_note: str = ""


@router.get("/platforms")
def list_platforms() -> dict[str, Any]:
    """The capability matrix, answered without touching the network."""
    items = []
    for adapter in registry.adapters:
        capabilities = adapter.capabilities
        items.append(
            {
                "slug": adapter.slug,
                "name": adapter.display_name,
                "category": adapter.category.value,
                "hosts": list(adapter.hosts),
                "capabilities": capabilities.as_dict(),
                "summary": capability_engine.summary(capabilities),
            }
        )
    return {
        "count": len(items),
        "platforms": items,
        "planned_endpoints": [{"method": m, "path": p, "phase": why} for m, p, why in PLANNED_ENDPOINTS],
    }


def _analyze_one(url: str) -> dict[str, Any]:
    detection = detector.detect(url)
    capabilities = capability_engine.for_detection(detection)

    if capabilities.metadata:
        status, note = "pending", "Metadata extraction lands in Phase 2."
    else:
        code = capabilities.reasons.get("metadata", "DRM_PROTECTED")
        status, note = "unavailable", (
            f"Metadata is not available for this source ({code}). "
            + ((detection.adapter.limitation_note and str(detection.adapter.limitation_note)) or "")
        ).strip()

    return {
        **detection.as_dict(),
        "capabilities": capabilities.as_dict(),
        "capability_summary": capability_engine.summary(capabilities),
        "metadata": None,
        "metadata_status": status,
        "metadata_note": note,
    }


@router.post("/analyze", response_model=AnalyzeOut)
def analyze(payload: Annotated[AnalyzeRequest, Body()]) -> AnalyzeOut:
    """Identify a URL and report what is authorised for it."""
    result = _analyze_one(payload.url)
    if payload.urls:
        # Batches return the same shape; the first URL drives the response and
        # the rest are validated so a bad one fails loudly rather than silently.
        for extra in payload.urls:
            _analyze_one(extra)
    return AnalyzeOut(**result)


@router.post("/analyze/batch")
def analyze_batch(payload: Annotated[AnalyzeRequest, Body()]) -> dict[str, Any]:
    """Per-URL results, where one bad URL does not fail the others."""
    urls = [payload.url, *payload.urls]
    results: list[dict[str, Any]] = []
    for url in urls:
        try:
            results.append({"ok": True, **_analyze_one(url)})
        except AppError as exc:
            results.append({"ok": False, "url": url, **exc.as_dict()})
    return {"count": len(results), "results": results}
