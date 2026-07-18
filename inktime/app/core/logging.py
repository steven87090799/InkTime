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

_ACTIVE_CONFIGURATION: tuple[str, str] | None = None


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


def configure_logging(
    format_name: str | None = None,
    level: str | None = None,
    *,
    settings_repository: Any | None = None,
    force: bool = False,
) -> tuple[str, str]:
    """設定單一 stdout handler；資料庫設定在啟動後優先於 bootstrap 環境變數。"""

    global _ACTIVE_CONFIGURATION
    repository_format = settings_repository.get("system.log_format", None) if settings_repository else None
    repository_level = settings_repository.get("system.log_level", None) if settings_repository else None
    selected = str(
        format_name or repository_format or os.environ.get("INKTIME_LOG_FORMAT") or "human"
    ).lower()
    selected_level = str(
        level or repository_level or os.environ.get("INKTIME_LOG_LEVEL") or "INFO"
    ).upper()
    if selected not in {"human", "json"}:
        selected = "human"
    if selected_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        selected_level = "INFO"
    configuration = (selected, selected_level)
    if not force and _ACTIVE_CONFIGURATION == configuration:
        return configuration

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if selected == "json" else HumanFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(selected_level)
    _ACTIVE_CONFIGURATION = configuration
    return configuration


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    logger.log(level, message, extra=redact(fields))
