"""PlateVision — application entrypoint.

Run locally:  uvicorn app.main:app --reload --port 8000
Production:   uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.router import api_router
from app.core.config import PROJECT_ROOT, settings
from app.core.errors import AppError, error_body
from app.core.logging import configure_logging, get_logger, set_request_context
from app.core.metrics import metrics
from app.core.ratelimit import TokenBucketLimiter
from app.db.session import SessionLocal
from app.services import bootstrap

configure_logging()
logger = get_logger("platevision")

STATIC_DIR = PROJECT_ROOT / "app" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting %s v%s (%s)", settings.app_name, settings.version, settings.environment)

    # Only the schema is created before we serve. Everything with an
    # unpredictable cost — demo seeding, glyph-bank construction — happens on a
    # background thread after the port is bound. Blocking startup on that work
    # is what made deploys fail with "no open ports detected": the free tier
    # takes minutes to seed, and the platform will not wait.
    try:
        bootstrap.create_schema(quiet=True)
    except Exception:  # pragma: no cover - surface the real error on first request
        logger.warning("Schema creation failed during startup", exc_info=True)

    app.state.bootstrap_thread = bootstrap.start_background_bootstrap(
        seed=settings.seed_demo_data,
        warm=True,
    )
    app.state.started_at = time.time()

    if settings.warnings:
        logger.warning(
            "Configuration advisories in %s: %s",
            settings.environment,
            ", ".join(settings.warnings),
        )
    logger.info(
        "Serving — %s backend, storage=%s; demo seed and engine warm-up running in "
        "the background. Docs at /docs, console at /",
        settings.backend_kind,
        settings.storage_backend,
    )
    yield
    logger.info("Shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{settings.app_name} API",
        description=(
            "Commercial licence-plate intelligence: recognition, watchlist "
            "enforcement, analytics and audit. Fully self-hosted."
        ),
        version=settings.version,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        root_path=settings.root_path,
    )
    app.state.limiter = TokenBucketLimiter(
        per_minute=settings.rate_limit_per_minute, burst=settings.rate_limit_burst
    )
    app.state.started_at = time.time()

    # Outermost first: browsers reject `*` + credentials, so wildcard CORS is
    # unavoidably credential-free. Same-origin console traffic is unaffected.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID", "X-Response-Time", "X-Plate-Best", "X-Plate-Count"],
        max_age=600,
    )
    app.add_middleware(GZipMiddleware, minimum_size=900)
    if settings.allowed_host_list != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)

    @app.middleware("http")
    async def instrumentation(request: Request, call_next):
        request_id = set_request_context(request.headers.get("x-request-id"))
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = (time.perf_counter() - started) * 1000
            metrics.inc("http_requests", status="500", path=_route_label(request))
            logger.exception(
                "Unhandled error", extra={"path": request.url.path, "method": request.method}
            )
            return JSONResponse(
                error_body("internal_error", "Unexpected server error.", request_id),
                status_code=500,
                headers={"X-Request-ID": request_id},
            )
        duration = (time.perf_counter() - started) * 1000
        path_label = _route_label(request)
        metrics.inc("http_requests", status=str(response.status_code), path=path_label)
        metrics.observe("http_request_duration", duration / 1000)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{duration:.1f}ms"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "font-src 'self' data:; connect-src 'self'; frame-ancestors *"
        )
        if request.url.path.startswith("/static/"):
            # Versioned-by-content assets are safe to cache; the shell is not.
            response.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
        elif request.url.path in ("/", "/index.html"):
            response.headers["Cache-Control"] = "no-cache"
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response

    # ------------------------------------------------------------- handlers
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        from app.core.logging import request_id_ctx

        return JSONResponse(
            error_body(exc.code, exc.message, request_id_ctx.get(), exc.details),
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        from app.core.logging import request_id_ctx

        # Raw pydantic errors can carry non-serialisable ctx objects.
        errors = [
            {
                "loc": ".".join(str(part) for part in error.get("loc", ())),
                "msg": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
            for error in exc.errors()[:8]
        ]
        return JSONResponse(
            error_body(
                "validation_failed",
                "Request validation failed.",
                request_id_ctx.get(),
                {"errors": errors},
            ),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        from app.core.logging import request_id_ctx

        if request.url.path.startswith(("/static", "/favicon")):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return JSONResponse(
            error_body(
                "http_error" if exc.status_code >= 400 else "ok",
                str(exc.detail),
                request_id_ctx.get(),
            ),
            status_code=exc.status_code,
        )

    # ---------------------------------------------------------------- routes
    app.include_router(api_router)

    @app.get("/api/v1/config", tags=["auth"], include_in_schema=True)
    def public_config() -> dict:
        """Unauthenticated runtime facts the sign-in screen needs.

        Deliberately excludes anything sensitive: no secret derivation, no user
        enumeration, no internal hostnames.
        """
        return {
            "app_name": settings.app_name,
            "tagline": settings.app_tagline,
            "version": settings.version,
            "environment": settings.environment,
            "demo_login": bool(settings.allow_demo_login),
            "demo_email": settings.demo_email if settings.allow_demo_login else None,
            "countries": settings.country_list,
            "max_upload_mb": settings.max_upload_mb,
        }

    @app.get("/healthz", include_in_schema=False)
    def liveness() -> dict:
        return {"status": "alive", "version": settings.version}

    @app.get("/readyz", include_in_schema=False)
    def readiness() -> PlainTextResponse:
        try:
            with SessionLocal() as db:
                db.execute(__import__("sqlalchemy").text("SELECT 1"))
            return PlainTextResponse("ready")
        except Exception:  # pragma: no cover
            return PlainTextResponse("not-ready", status_code=503)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
    def favicon():
        icon = STATIC_DIR / "img" / "favicon.svg"
        if icon.exists():
            return FileResponse(icon, media_type="image/svg+xml")
        return JSONResponse({"detail": "not found"}, status_code=404)

    return app


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    # Collapse ids so metric cardinality stays bounded.
    parts = []
    for part in path.split("/"):
        parts.append("{id}" if len(part) >= 16 and part.isalnum() else part)
    return "/".join(parts)


app = create_app()
