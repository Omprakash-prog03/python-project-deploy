"""Signed outbound webhooks for downstream systems (LPR-to-action pipelines)."""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import sign_payload
from app.models.entities import WebhookEndpoint

logger = get_logger(__name__)


def dispatch(db: Session, org_id: str, event: str, payload: dict) -> int:
    """Deliver an event to every active endpoint subscribed to it."""
    endpoints = db.scalars(
        select(WebhookEndpoint).where(
            WebhookEndpoint.org_id == org_id,
            WebhookEndpoint.active.is_(True),
        )
    ).all()
    targets = [e for e in endpoints if not e.events or event in e.events]
    if not targets:
        return 0
    body = json.dumps(
        {
            "event": event,
            "sent_at": datetime.now(UTC).isoformat(),
            "data": payload,
        },
        default=str,
    ).encode()
    delivered = 0
    with httpx.Client(timeout=settings.webhook_timeout_seconds) as client:
        for endpoint in targets:
            signature = sign_payload(endpoint.secret, body)
            headers = {
                "Content-Type": "application/json",
                "X-PlateVision-Event": event,
                "X-PlateVision-Signature": f"sha256={signature}",
                "User-Agent": "PlateVision-Webhook/1.0",
            }
            for attempt in range(1, max(1, settings.webhook_max_attempts) + 1):
                try:
                    response = client.post(endpoint.url, content=body, headers=headers)
                    endpoint.last_status = response.status_code
                    endpoint.last_delivery_at = datetime.now(UTC)
                    if response.status_code < 300:
                        endpoint.failure_count = 0
                        delivered += 1
                        break
                    endpoint.failure_count += 1
                except Exception as exc:
                    endpoint.failure_count += 1
                    logger.warning("Webhook %s attempt %d failed: %s", endpoint.id, attempt, exc)
                    time.sleep(min(2.0, 0.25 * attempt))
            db.add(endpoint)
    db.commit()
    return delivered
