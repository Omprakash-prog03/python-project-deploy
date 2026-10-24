"""Append-only audit trail (required for SOC2-style evidence and disputes)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import request_id_ctx
from app.models.entities import AuditEvent


def record(
    db: Session,
    *,
    org_id: str,
    actor: str,
    action: str,
    entity_type: str = "",
    entity_id: str = "",
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
    commit: bool = True,
) -> AuditEvent:
    event = AuditEvent(
        org_id=org_id,
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=detail or {},
        ip=ip,
        request_id=request_id_ctx.get(),
    )
    db.add(event)
    if commit:
        db.commit()
    return event
