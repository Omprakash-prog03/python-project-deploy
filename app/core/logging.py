"""Structured logging with request correlation ids."""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import ClassVar

from app.core.config import settings

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
_user_ctx: ContextVar[str] = ContextVar("user", default="-")


def set_request_context(request_id: str | None = None, user: str | None = None) -> str:
    rid = request_id or uuid.uuid4().hex[:12]
    request_id_ctx.set(rid)
    if user is not None:
        _user_ctx.set(user)
    return rid


class JsonFormatter(logging.Formatter):
    """Single-line JSON logs, friendly for Loki / CloudWatch / Datadog."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_ctx.get(),
            "user": _user_ctx.get(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key in ("path", "method", "status", "duration_ms", "plate", "camera_id"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    COLORS: ClassVar[dict[str, str]] = {
        "DEBUG": "\033[38;5;245m",
        "INFO": "\033[38;5;117m",
        "WARNING": "\033[38;5;221m",
        "ERROR": "\033[38;5;203m",
        "CRITICAL": "\033[1;38;5;197m",
    }
    RESET: ClassVar[str] = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        rid = request_id_ctx.get()
        base = (
            f"{color}{record.levelname:<8}{self.RESET} {ts} "
            f"\033[38;5;240m{rid:<12}\033[0m {record.name:<26} {record.getMessage()}"
        )
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    if settings.is_production:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(ConsoleFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if settings.debug else logging.INFO)
    for noisy in ("uvicorn.access", "multipart", "PIL", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").handlers = root.handlers


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
