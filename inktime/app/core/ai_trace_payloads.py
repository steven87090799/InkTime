from __future__ import annotations

import json
import re
from typing import Any

from inktime.app.core.security import redact_text


TRACE_REDACTED = "[已遮蔽]"
IMAGE_PAYLOAD_REDACTED = "[已遮蔽圖片資料]"
TRUNCATED = "[已截斷]"

_SECRET_KEY = re.compile(
    r"(?:authorization|api[_-]?key|x[_-]?api[_-]?key|cookie|session[_-]?cookie|password|"
    r"secret|credential|access[_-]?token|refresh[_-]?token|client[_-]?secret|bearer)",
    re.IGNORECASE,
)
_DATA_IMAGE = re.compile(
    r"data:image/[^;,\s]+;base64,[A-Za-z0-9+/=]+",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|x[_-]?api[_-]?key|authorization|cookie|password|secret|credential|"
    r"access[_-]?token|refresh[_-]?token|client[_-]?secret)\b\s*[:=]\s*)([^\s,;&]+)"
)


def _bounded(value: Any, *, depth: int, maximum_depth: int) -> Any:
    if depth >= maximum_depth:
        return {"_truncated": True, "reason": "maximum_depth"}
    if isinstance(value, dict):
        mapping_result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 100:
                mapping_result["_truncated"] = True
                mapping_result["_truncated_reason"] = "maximum_items"
                break
            name = str(key)[:160]
            mapping_result[name] = (
                TRACE_REDACTED
                if _SECRET_KEY.search(name)
                else _bounded(item, depth=depth + 1, maximum_depth=maximum_depth)
            )
        return mapping_result
    if isinstance(value, (list, tuple)):
        sequence_result = [
            _bounded(item, depth=depth + 1, maximum_depth=maximum_depth) for item in value[:100]
        ]
        if len(value) > 100:
            sequence_result.append({"_truncated": True, "reason": "maximum_items"})
        return sequence_result
    if isinstance(value, bytes):
        return IMAGE_PAYLOAD_REDACTED
    if isinstance(value, str):
        without_images = _DATA_IMAGE.sub(IMAGE_PAYLOAD_REDACTED, value)
        cleaned = redact_text(
            _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{TRACE_REDACTED}", without_images)
        )
        return cleaned if len(cleaned) <= 16_000 else cleaned[:16_000] + TRUNCATED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))[:16_000]


def sanitize_trace_value(value: Any, *, maximum_depth: int = 8) -> Any:
    """Apply the platform redactor plus strict Trace-specific payload bounds."""

    return _bounded(value, depth=0, maximum_depth=max(2, min(maximum_depth, 12)))


def bounded_json_text(value: Any, *, maximum_bytes: int = 65_536) -> str:
    clean = sanitize_trace_value(value)
    encoded = json.dumps(clean, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) <= maximum_bytes:
        return encoded
    marker = {
        "_truncated": True,
        "reason": "maximum_serialized_bytes",
        "original_bytes": len(encoded.encode("utf-8")),
    }
    return json.dumps(marker, ensure_ascii=False, separators=(",", ":"))


def bounded_text(value: Any, *, maximum_bytes: int = 65_536) -> str:
    raw = str(value or "")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        clean = str(sanitize_trace_value(raw))
    else:
        clean = json.dumps(
            sanitize_trace_value(parsed),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    encoded = clean.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return clean
    suffix = TRUNCATED.encode("utf-8")
    return encoded[: max(0, maximum_bytes - len(suffix))].decode("utf-8", "ignore") + TRUNCATED
