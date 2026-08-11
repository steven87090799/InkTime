from __future__ import annotations

import json
import re
from typing import Any

from inktime.app.core.security import redact_text


IMAGE_PAYLOAD_REDACTED = "[IMAGE_PAYLOAD_REDACTED]"
SECRET_REDACTED = "[REDACTED]"  # noqa: S105 -- literal replacement marker, not a credential
MAX_TRACE_TEXT_CHARS = 128 * 1024
MAX_TRACE_DEPTH = 20

_SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:authorization|authentication|api[_-]?key|password|cookies?|bearer|session(?:_id)?|client[_-]?secret|"
    r"access[_-]?token|refresh[_-]?token|credential|secret|token)(?:$|[_-])",
    re.IGNORECASE,
)
_IMAGE_DATA = re.compile(r"^data:image/[^;]+;base64,", re.IGNORECASE)
_LONG_BASE64 = re.compile(r"^[A-Za-z0-9+/]{256,}={0,2}$")


def _bounded_text(value: Any) -> str:
    original = str(value)
    if _IMAGE_DATA.match(original) or _LONG_BASE64.match(original):
        return IMAGE_PAYLOAD_REDACTED
    text = redact_text(original)
    if len(text) <= MAX_TRACE_TEXT_CHARS:
        return text
    return text[:MAX_TRACE_TEXT_CHARS] + "\n[TRUNCATED]"


def _image_reference(value: dict[str, Any], photo: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "input_image"}
    for source, target in (
        ("id", "photo_id"),
        ("sha256", "sha256"),
        ("width", "width"),
        ("height", "height"),
        ("mime_type", "mime_type"),
    ):
        metadata = (photo or {}).get(source)
        if metadata in (None, ""):
            metadata = value.get(target)
        if metadata not in (None, ""):
            result[target] = metadata
    image_url = value.get("image_url")
    if isinstance(image_url, dict) and image_url.get("detail") is not None:
        result["detail"] = _bounded_text(image_url["detail"])
    elif value.get("detail") is not None:
        result["detail"] = _bounded_text(value["detail"])
    result["payload"] = IMAGE_PAYLOAD_REDACTED
    return result


def sanitize_ai_payload(
    value: Any,
    *,
    photo: dict[str, Any] | None = None,
    _depth: int = 0,
) -> Any:
    """Return a bounded trace representation with credentials and image bytes removed."""

    if _depth >= MAX_TRACE_DEPTH:
        return "[MAX_DEPTH]"
    if isinstance(value, dict):
        item_type = str(value.get("type") or "").casefold()
        if item_type in {"image_url", "input_image", "image"}:
            return _image_reference(value, photo)
        clean: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _SECRET_KEY.search(name) and name.casefold() not in {
                "max_tokens",
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "reasoning_tokens",
                "cache_write_tokens",
                "total_tokens",
            }:
                clean[name] = SECRET_REDACTED
            else:
                clean[name] = sanitize_ai_payload(item, photo=photo, _depth=_depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [sanitize_ai_payload(item, photo=photo, _depth=_depth + 1) for item in value]
    if isinstance(value, bytes):
        return IMAGE_PAYLOAD_REDACTED
    if isinstance(value, str):
        return _bounded_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _bounded_text(value)


def sanitized_json_text(value: Any, *, photo: dict[str, Any] | None = None) -> str:
    payload = sanitize_ai_payload(value, photo=photo)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    if len(text) <= MAX_TRACE_TEXT_CHARS:
        return text
    return json.dumps(
        {"truncated": True, "preview": text[:MAX_TRACE_TEXT_CHARS]},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def sanitized_response_text(value: Any) -> str:
    if isinstance(value, str):
        return _bounded_text(value)
    return sanitized_json_text(value)
