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

from app.api.router import api_router
from app.core.config import PROJECT_ROOT, settings
from app.core.errors import AppError, error_body
from app.core.logging import configure_logging, get_logger, set_request_context
from app.core.metrics import metrics
from app.core.ratelimit import TokenBucketLimiter
from app.db.session import Base, SessionLocal, engine

configure_logging()
logger = get_logger("platevision")

STATIC_DIR = PROJECT_ROOT / "app" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting %s v%s (%s)", settings.app_name, settings.version, settings.environment)
    Base.metadata.create_all(bind=engine)
    if settings.seed_demo_data:
        try:
            from app.seed import seed_if_empty

            with SessionLocal() as db:
                created = seed_if_empty(db)
            if created:
                logger.info("Demo workspace provisioned")
        except Exception:  # pragma: no cover - never block startup on demo data
            logger.warning("Demo seeding skipped", exc_info=True)
    # Warm the glyph bank so the first recognition request is fast.
    try:
        from app.alpr.ocr import get_classifier

        get_classifier()._ensure()
    except Exception:  # pragma: no cover
        logger.warning("Glyph bank warm-up failed", exc_info=True)
    app.state.started_at = time.time()
    logger.info("Ready — docs at /docs, console at /")
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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Response-Time", "X-Plate-Best"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=900)

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

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/favicon.ico", include_in_schema=False)
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
