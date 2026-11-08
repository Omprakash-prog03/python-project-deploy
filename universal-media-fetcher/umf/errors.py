"""Error taxonomy.

§12 of the spec asks for understandable errors. The important property is that
a limitation and a fault are different things: a Netflix URL is not invalid, a
DRM-protected stream is not a network error, and a platform that forbids
downloads has not malfunctioned. Collapsing those into "failed" is what makes
downloader apps feel broken and is the single most common flaw in this category.

Every code maps to a stable HTTP status and a user-facing sentence, so the API
can be honest without the frontend inventing copy.
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """Machine-readable outcomes. Values match the spec's §12 vocabulary."""

    INVALID_URL = "INVALID_URL"
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    SUBSCRIPTION_REQUIRED = "SUBSCRIPTION_REQUIRED"
    DRM_PROTECTED = "DRM_PROTECTED"
    DOWNLOAD_NOT_PERMITTED = "DOWNLOAD_NOT_PERMITTED"
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
    NETWORK_ERROR = "NETWORK_ERROR"
    UNAVAILABLE = "UNAVAILABLE"

    #: Extensions beyond the spec's list, needed by real adapters.
    PRIVATE_CONTENT = "PRIVATE_CONTENT"
    GEO_BLOCKED = "GEO_BLOCKED"
    RATE_LIMITED = "RATE_LIMITED"
    URL_TOO_LARGE = "URL_TOO_LARGE"


#: How each outcome maps onto HTTP. A limitation is not a server fault, so it
#: must not be a 5xx: that would page an operator for a Netflix link.
HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_URL: 400,
    ErrorCode.UNSUPPORTED_PLATFORM: 422,
    ErrorCode.AUTHENTICATION_REQUIRED: 401,
    ErrorCode.SUBSCRIPTION_REQUIRED: 402,
    ErrorCode.DRM_PROTECTED: 451,          # Unavailable For Legal Reasons
    ErrorCode.DOWNLOAD_NOT_PERMITTED: 403,
    ErrorCode.TEMPORARY_FAILURE: 503,
    ErrorCode.NETWORK_ERROR: 502,
    ErrorCode.UNAVAILABLE: 404,
    ErrorCode.PRIVATE_CONTENT: 403,
    ErrorCode.GEO_BLOCKED: 451,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.URL_TOO_LARGE: 413,
}

#: Plain-language explanation, shown verbatim to users.
MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.INVALID_URL: "The supplied URL is invalid.",
    ErrorCode.UNSUPPORTED_PLATFORM: "This platform is not currently integrated.",
    ErrorCode.AUTHENTICATION_REQUIRED: (
        "This content needs you to sign in through the platform's official authorization."
    ),
    ErrorCode.SUBSCRIPTION_REQUIRED: "An active subscription is required to access this content.",
    ErrorCode.DRM_PROTECTED: (
        "This content is protected. Direct file extraction is unavailable through this integration."
    ),
    ErrorCode.DOWNLOAD_NOT_PERMITTED: "The source does not authorize downloading this content.",
    ErrorCode.TEMPORARY_FAILURE: "The source is temporarily unavailable. Try again shortly.",
    ErrorCode.NETWORK_ERROR: "Unable to connect to the source.",
    ErrorCode.UNAVAILABLE: "This content is not available. It may have been removed or made private.",
    ErrorCode.PRIVATE_CONTENT: "This content is private and cannot be accessed.",
    ErrorCode.GEO_BLOCKED: "This content is not available in your region.",
    ErrorCode.RATE_LIMITED: "The source is rate-limiting requests. Try again shortly.",
    ErrorCode.URL_TOO_LARGE: "This URL is too large to process.",
}

#: A legitimate thing the user can do instead. Never a dead end: §2 requires an
#: authorized alternative wherever an operation is refused.
ALTERNATIVES: dict[ErrorCode, str] = {
    ErrorCode.AUTHENTICATION_REQUIRED: "Connect your account for this platform first.",
    ErrorCode.SUBSCRIPTION_REQUIRED: "Subscribe on the platform, or watch where it is included.",
    ErrorCode.DRM_PROTECTED: "Open the title in the provider's official app to watch or download it there.",
    ErrorCode.DOWNLOAD_NOT_PERMITTED: "Use the platform's own download or save feature, where it offers one.",
    ErrorCode.PRIVATE_CONTENT: "Ask the owner for access via the platform.",
    ErrorCode.GEO_BLOCKED: "Use the official app in a region where the title is licensed.",
}


class AppError(Exception):
    """A refusal or a fault, carrying a stable code and a user-facing message."""

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
        alternative: str | None = None,
    ) -> None:
        self.code = code
        self.user_message = message or MESSAGES.get(code, "Something went wrong.")
        self.detail = detail or {}
        self.alternative = alternative or ALTERNATIVES.get(code)
        super().__init__(f"{code.value}: {self.user_message}")

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 500)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": {
                "code": self.code.value,
                "message": self.user_message,
            }
        }
        if self.alternative:
            payload["error"]["alternative"] = self.alternative
        if self.detail:
            payload["error"]["detail"] = self.detail
        return payload


class Unsupported(Exception):
    """Raised by adapter methods that the platform cannot support.

    Distinct from :class:`AppError` on purpose. ``AppError`` means "the request
    could not be completed"; ``Unsupported`` means "this platform does not offer
    this operation, and that is a permanent fact about the platform".
    """

    def __init__(self, code: ErrorCode, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"{code.value}: {reason}")
