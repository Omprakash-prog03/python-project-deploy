"""API v1 router aggregation."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
    analytics,
    auth,
    cameras,
    captures,
    recognize,
    system,
    watchlist,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(captures.router)
api_router.include_router(recognize.router)
api_router.include_router(watchlist.router)
api_router.include_router(analytics.router)
api_router.include_router(cameras.router)
api_router.include_router(system.router)
