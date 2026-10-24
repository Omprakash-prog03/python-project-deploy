"""Stateless recognition endpoints (no persistence) for kiosks and integrations."""
from __future__ import annotations

import base64
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile

from app.alpr import imaging
from app.alpr.engine import EngineOptions, get_engine
from app.api.deps import CurrentUser, Org, enforce_rate_limit
from app.core.errors import ValidationFailed
from app.schemas.models import PlateOut, RecognitionOut
from app.services import samples
from app.services.annotate import annotate
from app.services.recognition import validate_upload

router = APIRouter(prefix="/recognize", tags=["recognition"], dependencies=[Depends(enforce_rate_limit)])


def _to_schema(result) -> RecognitionOut:
    def plate_out(plate) -> PlateOut:
        return PlateOut(
            plate=plate.plate,
            raw_text=plate.raw_text,
            confidence=round(plate.confidence, 4),
            box=plate.box.as_dict(),
            quad=plate.quad,
            country=plate.country,
            region=plate.region,
            plate_type=plate.plate_type,
            format=plate.format_name,
            format_valid=plate.format_valid,
            alternates=plate.alternates,
            diagnostics=plate.diagnostics,
            glyphs=[
                {
                    "label": g.label,
                    "confidence": round(g.confidence, 4),
                    "box": g.box.as_dict(),
                }
                for g in plate.glyphs
            ],
        )

    return RecognitionOut(
        plates=[plate_out(p) for p in result.plates],
        best=plate_out(result.best) if result.best else None,
        image={"width": result.image_width, "height": result.image_height},
        processing_ms=round(result.processing_ms, 2),
        timings={k: round(v, 2) for k, v in result.timings.items()},
        engine_version=result.engine_version,
        diagnostics=result.diagnostics,
    )


@router.post("", response_model=RecognitionOut)
async def recognize(
    user: CurrentUser,
    org: Org,
    file: Annotated[UploadFile, File(description="Vehicle frame")],
    country: Annotated[str | None, Form()] = None,
    max_candidates: Annotated[int, Form(ge=1, le=6)] = 4,
    include_overlay: Annotated[bool, Form()] = False,
) -> RecognitionOut:
    data = await file.read()
    validate_upload(data)
    result = get_engine().recognize(
        data, EngineOptions(country=country, max_candidates=max_candidates)
    )
    payload = _to_schema(result)
    if include_overlay:
        overlay = annotate(imaging.load_image(data), result)
        payload.diagnostics["overlay_b64"] = (
            "data:image/jpeg;base64," + base64.b64encode(overlay).decode()
        )
    return payload


@router.post("/annotated")
async def recognize_annotated(
    user: CurrentUser,
    org: Org,
    file: Annotated[UploadFile, File()],
    country: Annotated[str | None, Form()] = None,
) -> Response:
    data = await file.read()
    validate_upload(data)
    result = get_engine().recognize(data, EngineOptions(country=country))
    overlay = annotate(imaging.load_image(data), result)
    return Response(
        content=overlay,
        media_type="image/jpeg",
        headers={
            "X-Plate-Count": str(len(result.plates)),
            "X-Plate-Best": result.best.plate if result.best else "",
            "X-Processing-Ms": f"{result.processing_ms:.1f}",
        },
    )


@router.get("/samples")
def list_samples(user: CurrentUser, limit: int = Query(6, ge=1, le=12)) -> dict:
    return {"items": samples.sample_gallery(limit)}


@router.get("/samples/{sample_id}.jpg")
def sample_image(sample_id: str) -> Response:
    data = samples.sample_bytes(sample_id)
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.post("/samples/{sample_id}")
def recognize_sample(
    sample_id: str,
    user: CurrentUser,
    org: Org,
    country: Annotated[str | None, Query()] = None,
    include_overlay: bool = Query(True),
) -> RecognitionOut:
    data = samples.sample_bytes(sample_id)
    if not data:
        raise ValidationFailed("Unknown sample.", code="sample_not_found")
    result = get_engine().recognize(data, EngineOptions(country=country))
    payload = _to_schema(result)
    if include_overlay:
        overlay = annotate(imaging.load_image(data), result)
        payload.diagnostics["overlay_b64"] = (
            "data:image/jpeg;base64," + base64.b64encode(overlay).decode()
        )
    return payload
