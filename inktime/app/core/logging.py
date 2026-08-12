from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from itertools import islice
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from typing import Any, Iterator, Mapping

from inktime.app.core.security import SENSITIVE_KEY, redact_text


SCHEMA_VERSION = 1
MAX_STRING_LENGTH = 2_048
MAX_EXCEPTION_LENGTH = 2_048
MAX_STACK_TRACE_LENGTH = 12_000
MAX_DETAILS_ITEMS = 64
MAX_DETAILS_DEPTH = 5

# Existing fields remain present while correlation and lifecycle fields extend
# the schema. Empty defaults give JSON consumers a stable query contract.
STANDARD_FIELDS = {
    "schema_version": SCHEMA_VERSION,
    "timestamp": "",
    "level": "",
    "component": "",
    "event": "",
    "message": "",
    "error_code": "",
    "trace_id": "",
    "request_id": "",
    "operation_id": "",
    "job_id": "",
    "job_item_id": "",
    "worker_id": "",
    "photo_id": "",
    "batch_id": "",
    "batch_item_id": "",
    "release_id": "",
    "device_id": "",
    "queue_id": "",
    "queue_item_id": "",
    "provider": "",
    "provider_id": "",
    "model": "",
    "provider_request_id": "",
    "task_key": "",
    "schedule_id": "",
    "stage": "",
    "phase": "",
    "operation": "",
    "attempt": 0,
    "retry_count": 0,
    "duration_ms": 0,
    "http_method": "",
    "http_status": 0,
    "failure_class": "",
    "retryable": False,
    "ambiguous": False,
    "process_role": "",
    "pid": 0,
    "thread_name": "",
    "exception_type": "",
    "exception_message": "",
    "stack_trace": "",
    "details": {},
}

_ACTIVE_CONFIGURATION: tuple[str, str] | None = None
_LOG_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "inktime_log_context", default=None
)
_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMIT_STATE: OrderedDict[str, float] = OrderedDict()
_MAX_RATE_LIMIT_KEYS = 256
_SAFE_CONTEXT_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _safe_text(value: Any, limit: int = MAX_STRING_LENGTH) -> str:
    try:
        return redact_text(str(value))[:limit]
    except Exception:
        return "[unavailable]"


def _bounded(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON-safe, centrally redacted and bounded diagnostic value."""

    try:
        if depth >= MAX_DETAILS_DEPTH:
            return "[truncated]"
        if isinstance(value, Mapping):
            bounded_mapping: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= MAX_DETAILS_ITEMS:
                    bounded_mapping["_truncated"] = True
                    break
                safe_key = _safe_text(key, 128)
                bounded_mapping[safe_key] = (
                    "[已遮蔽]"
                    if SENSITIVE_KEY.search(safe_key)
                    else _bounded(item, depth=depth + 1)
                )
            return bounded_mapping
        if isinstance(value, (list, tuple, set)):
            values = list(islice(iter(value), MAX_DETAILS_ITEMS + 1))
            bounded_items: list[Any] = [
                _bounded(item, depth=depth + 1) for item in values[:MAX_DETAILS_ITEMS]
            ]
            if len(values) > MAX_DETAILS_ITEMS:
                bounded_items.append("[truncated]")
            return bounded_items
        if isinstance(value, str):
            return _safe_text(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return _safe_text(value)
    except Exception:
        return "[unavailable]"


def bind_log_context(**fields: Any) -> Token:
    """Bind bounded correlation fields for the current execution context."""

    current = dict(_LOG_CONTEXT.get() or {})
    for key, value in fields.items():
        if key in STANDARD_FIELDS and _SAFE_CONTEXT_KEY.fullmatch(key):
            current[key] = _bounded(value)
    return _LOG_CONTEXT.set(current)


def get_log_context() -> dict[str, Any]:
    return dict(_LOG_CONTEXT.get() or {})


def clear_log_context(token: Token | None = None) -> None:
    if token is not None:
        _LOG_CONTEXT.reset(token)
    else:
        _LOG_CONTEXT.set({})


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    token = bind_log_context(**fields)
    try:
        yield
    finally:
        clear_log_context(token)


def should_log_sample(index: int, *, first: int = 3, every: int = 100) -> bool:
    """Bound high-cardinality loop diagnostics without per-entity state."""

    normalized = max(0, int(index))
    return normalized < max(0, int(first)) or (
        every > 0 and normalized > 0 and normalized % int(every) == 0
    )


def should_log_rate_limited(
    key: str,
    *,
    interval_seconds: float = 60.0,
    now: float | None = None,
) -> bool:
    """Allow one event per bounded key and interval, evicting oldest keys."""

    current = time.monotonic() if now is None else float(now)
    safe_key = _safe_text(key, 128)
    with _RATE_LIMIT_LOCK:
        last = _RATE_LIMIT_STATE.get(safe_key)
        if last is not None and current - last < max(0.0, float(interval_seconds)):
            return False
        _RATE_LIMIT_STATE[safe_key] = current
        _RATE_LIMIT_STATE.move_to_end(safe_key)
        while len(_RATE_LIMIT_STATE) > _MAX_RATE_LIMIT_KEYS:
            _RATE_LIMIT_STATE.popitem(last=False)
    return True


def _safe_exception(record: logging.LogRecord) -> tuple[str, str, str]:
    if not record.exc_info:
        return "", "", ""
    exc_type, exc, _tb = record.exc_info
    type_name = getattr(exc_type, "__name__", type(exc).__name__ if exc else "Exception")
    message = _safe_text(exc or "", MAX_EXCEPTION_LENGTH)
    try:
        rendered = "".join(traceback.format_exception(*record.exc_info))
    except Exception:
        rendered = f"{type_name}: {message}"
    return _safe_text(type_name, 256), message, _safe_text(rendered, MAX_STACK_TRACE_LENGTH)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        try:
            exception_type, exception_message, stack_trace = _safe_exception(record)
            payload = dict(STANDARD_FIELDS)
            payload.update(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "level": record.levelname.lower(),
                    "component": getattr(record, "component", record.name),
                    "event": getattr(record, "event", "log"),
                    "message": _safe_text(record.getMessage()),
                    "process_role": getattr(
                        record, "process_role", os.environ.get("INKTIME_PROCESS_ROLE", "")
                    ),
                    "pid": getattr(record, "pid", record.process),
                    "thread_name": getattr(record, "thread_name", record.threadName),
                    "exception_type": getattr(record, "exception_type", exception_type),
                    "exception_message": getattr(record, "exception_message", exception_message),
                    "stack_trace": getattr(record, "stack_trace", stack_trace),
                }
            )
            for key in STANDARD_FIELDS:
                if key in {"schema_version", "timestamp", "level", "component", "message"}:
                    continue
                if hasattr(record, key):
                    payload[key] = getattr(record, key)
            return json.dumps(_bounded(payload), ensure_ascii=False, default=str)
        except Exception:
            return '{"level":"error","event":"logging_format_failed","message":"[unavailable]"}'


class HumanFormatter(logging.Formatter):
    _IDENTIFIERS = (
        ("event", "event"),
        ("error_code", "code"),
        ("request_id", "request"),
        ("job_id", "job"),
        ("job_item_id", "item"),
        ("photo_id", "photo"),
        ("batch_id", "batch"),
        ("release_id", "release"),
        ("device_id", "device"),
        ("provider", "provider"),
        ("stage", "stage"),
        ("attempt", "attempt"),
        ("duration_ms", "duration_ms"),
    )

    def format(self, record: logging.LogRecord) -> str:
        try:
            component = getattr(record, "component", record.name)
            prefix = f"[{record.levelname}] [{_safe_text(component, 128)}]"
            parts = []
            for field, label in self._IDENTIFIERS:
                value = getattr(record, field, "")
                if value is not None and value != "" and value != 0 and value is not False:
                    if field in {"event", "error_code"}:
                        parts.append(f"[{_safe_text(value, 128)}]")
                    else:
                        parts.append(f"{label}={_safe_text(value, 256)}")
            _type, _message, stack_trace = _safe_exception(record)
            rendered = f"{prefix}{(' ' + ' '.join(parts)) if parts else ''} "
            rendered += _safe_text(record.getMessage())
            if stack_trace and record.levelno >= logging.ERROR:
                rendered += "\n" + stack_trace
            return rendered
        except Exception:
            return "[ERROR] [logging] [logging_format_failed] [unavailable]"


def configure_logging(
    format_name: str | None = None,
    level: str | None = None,
    *,
    settings_repository: Any | None = None,
    force: bool = False,
) -> tuple[str, str]:
    """Configure one stdout handler; persisted settings override bootstrap env."""

    global _ACTIVE_CONFIGURATION
    repository_values = (
        settings_repository.get_many(
            ["system.log_format", "system.log_level"],
            defaults={"system.log_format": None, "system.log_level": None},
        )
        if settings_repository
        else {}
    )
    repository_format = repository_values.get("system.log_format")
    repository_level = repository_values.get("system.log_level")
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
    """Emit one structured event; diagnostics must never break the operation."""

    try:
        if not logger.isEnabledFor(level):
            return
        exc_info = fields.pop("exc_info", None)
        merged = get_log_context()
        merged.update(fields)
        safe_fields = {
            key: _bounded(value)
            for key, value in merged.items()
            if key in STANDARD_FIELDS and key not in {"timestamp", "level", "message"}
        }
        safe_fields.setdefault("component", logger.name)
        logger.log(level, _safe_text(message), extra=safe_fields, exc_info=exc_info)
    except Exception:
        # Never turn diagnostics into a production failure or recursively log.
        return
