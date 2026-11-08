"""Universal Media Fetcher -- application entry point.

Phase 0: identification and capability reporting. No outbound requests are made
by any route in this application yet; the SSRF policy is in place so that the
fetcher added in Phase 1 has one door to walk through.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from umf.api.routes import router
from umf.core.registry import registry
from umf.errors import AppError

app = FastAPI(
    title="Universal Media Fetcher",
    version="0.1.0",
    description=(
        "Capability-aware media identification. Every platform is an independent "
        "adapter; operations a platform does not authorise are reported honestly "
        "rather than attempted."
    ),
)

app.include_router(router)


@app.exception_handler(AppError)
def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    """Every refusal carries a stable code, a sentence, and an alternative."""
    return JSONResponse(status_code=exc.http_status, content=exc.as_dict())


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok", "version": app.version, "adapters": len(registry)}


@app.get("/api/v1/info")
def info() -> dict:
    """What this build does and does not do, stated in the API."""
    return {
        "name": "Universal Media Fetcher",
        "version": app.version,
        "phase": 0,
        "implemented": [
            "URL identification across 21 platforms and direct media",
            "SSRF policy (scheme, port, credentials, IP ranges, DNS answers, redirects)",
            "capability profiles with machine-readable refusals",
        ],
        "not_implemented": [
            "metadata retrieval (Phase 2)",
            "downloads (Phase 1)",
            "authentication and accounts (Phase 4)",
            "the web interface",
        ],
        "makes_outbound_requests": False,
    }
