"""Analytics endpoints powering the dashboard."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentUser, DbSession, Org, enforce_rate_limit
from app.schemas.models import OverviewOut
from app.services import analytics

router = APIRouter(prefix="/analytics", tags=["analytics"], dependencies=[Depends(enforce_rate_limit)])


@router.get("/overview", response_model=OverviewOut)
def overview(
    db: DbSession,
    user: CurrentUser,
    org: Org,
    range: str = Query("7d", pattern="^(24h|7d|30d|90d)$"),
    tz_offset: int = Query(0, ge=-720, le=840, description="Minutes east of UTC"),
) -> OverviewOut:
    return OverviewOut(**analytics.overview(db, org, range, tz_offset))


@router.get("/heatmap")
def heatmap(db: DbSession, user: CurrentUser, org: Org, days: int = Query(7, ge=1, le=30)) -> dict:
    return {"cells": analytics.heatmap(db, org, days), "days": days}
