"""The capability engine.

§5 asks the application to distinguish nine outcomes that most downloaders
collapse into one. This module is where that distinction is made, and it is the
commercial core of the product: telling a user *why* an operation is
unavailable, and what to do instead, is more useful than a download button that
fails.

The engine answers three questions separately, because they have different
answers:

1. **What can this platform do at all?** Static, from the adapter.
2. **What can it do for this URL?** Refined by the adapter.
3. **Is the refusal a limitation or a fault?** From the error taxonomy.
"""
from __future__ import annotations

from dataclasses import replace

from umf.core.detector import Detection, content_ref
from umf.core.models import Capabilities
from umf.errors import AppError, ErrorCode

#: Capabilities a caller may ask about, in display order.
CAPABILITY_NAMES = (
    "metadata",
    "preview",
    "official_playback",
    "authorized_download",
    "subtitle_access",
)


class CapabilityEngine:
    """Computes the capability profile for a detected URL."""

    def for_detection(self, detection: Detection) -> Capabilities:
        refined = detection.adapter.refine_capabilities(content_ref(detection))

        notes = list(refined.notes)

        reasons = dict(refined.reasons)
        for name in CAPABILITY_NAMES:
            if getattr(refined, name):
                reasons.pop(name, None)

        return replace(refined, reasons=reasons, notes=tuple(notes))

    def summary(self, capabilities: Capabilities) -> dict:
        """Human-facing summary used by the analysis screen."""
        available = [name for name in CAPABILITY_NAMES if getattr(capabilities, name)]
        unavailable = [name for name in CAPABILITY_NAMES if not getattr(capabilities, name)]
        return {
            "available": available,
            "unavailable": [
                {"capability": name, "code": capabilities.reasons.get(name, "DOWNLOAD_NOT_PERMITTED")}
                for name in unavailable
            ],
        }

    def require(self, capabilities: Capabilities, capability: str) -> None:
        """Raise the specific refusal, not a generic failure.

        Called by any endpoint that needs a capability. Raising here -- with the
        platform's own reason code -- is what keeps a Netflix link from
        surfacing as a 500 or as "invalid URL".
        """
        if not getattr(capabilities, capability, False):
            code = capabilities.reasons.get(capability)
            if code:
                try:
                    parsed = ErrorCode(code)
                except ValueError:
                    parsed = ErrorCode.DOWNLOAD_NOT_PERMITTED
            else:
                parsed = ErrorCode.DOWNLOAD_NOT_PERMITTED
            raise AppError(parsed, detail={"capability": capability})


engine = CapabilityEngine()
