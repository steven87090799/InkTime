from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import sys
from typing import Any

from inktime.app.core.security import redact


STANDARD_FIELDS = {
    "timestamp": "",
    "level": "",
    "component": "",
    "event": "",
    "error_code": "",
    "message": "",
    "job_id": "",
    "photo_id": "",
    "provider": "",
    "model": "",
    "duration_ms": 0,
    "retry_count": 0,
    "details": {},
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = dict(STANDARD_FIELDS)
        payload.update(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname.lower(),
                "component": getattr(record, "component", record.name),
                "event": getattr(record, "event", "log"),
                "error_code": getattr(record, "error_code", ""),
                "message": record.getMessage(),
                "job_id": getattr(record, "job_id", ""),
                "photo_id": getattr(record, "photo_id", ""),
                "provider": getattr(record, "provider", ""),
                "model": getattr(record, "model", ""),
                "duration_ms": getattr(record, "duration_ms", 0),
                "retry_count": getattr(record, "retry_count", 0),
                "details": getattr(record, "details", {}),
            }
        )
        return json.dumps(redact(payload), ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        error_code = getattr(record, "error_code", "")
        prefix = f"[{record.levelname}] [{record.name}]"
        return f"{prefix}{f' [{error_code}]' if error_code else ''} {record.getMessage()}"


def configure_logging(format_name: str | None = None, level: str | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    selected = (format_name or os.environ.get("INKTIME_LOG_FORMAT") or "human").lower()
    handler.setFormatter(JsonFormatter() if selected == "json" else HumanFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel((level or os.environ.get("INKTIME_LOG_LEVEL") or "INFO").upper())


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    logger.log(level, message, extra=redact(fields))
