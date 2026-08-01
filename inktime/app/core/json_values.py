from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

from werkzeug.exceptions import BadRequest, RequestEntityTooLarge, UnsupportedMediaType


class JsonScalarError(ValueError):
    pass


_MISSING = object()


def json_object_payload(
    request: Any,
    *,
    maximum_bytes: int,
    error_prefix: str,
) -> dict[str, Any]:
    """Load one bounded JSON object without treating falsey scalars as {}."""
    if maximum_bytes < 1:
        raise ValueError("maximum_bytes 必須大於 0")
    if request.content_length is not None and request.content_length > maximum_bytes:
        raise RequestEntityTooLarge(description=f"{error_prefix} JSON Payload 過大")
    if not request.is_json:
        raise UnsupportedMediaType(description=f"{error_prefix} Content-Type 必須是 application/json")
    # Werkzeug enforces this while reading even when Content-Length is absent
    # (for example, a chunked request), so the boundary cannot buffer an
    # unbounded body before applying the per-endpoint limit.
    request.max_content_length = maximum_bytes
    raw = request.get_data(cache=True)
    if len(raw) > maximum_bytes:
        raise RequestEntityTooLarge(description=f"{error_prefix} JSON Payload 過大")
    try:
        payload = request.get_json(silent=False)
    except BadRequest as exc:
        raise BadRequest(description=f"{error_prefix} JSON 格式錯誤") from exc
    if type(payload) is not dict:
        raise BadRequest(description=f"{error_prefix} JSON Payload 必須是物件")
    return payload


def _message(error_prefix: str, field: str, expectation: str) -> JsonScalarError:
    return JsonScalarError(f"{error_prefix} {field} {expectation}")


def require_json_bool(
    payload: Mapping[str, Any],
    field: str,
    *,
    error_prefix: str = "JSON-001",
) -> bool:
    if field not in payload or type(payload[field]) is not bool:
        raise _message(error_prefix, field, "必須是 JSON Boolean")
    return payload[field]


def json_bool(
    payload: Mapping[str, Any],
    field: str,
    *,
    default: Any = _MISSING,
    required: bool = False,
    error_prefix: str = "JSON-001",
) -> bool:
    if field not in payload:
        if required or default is _MISSING:
            raise _message(error_prefix, field, "為必填欄位")
        return bool(default)
    return require_json_bool(payload, field, error_prefix=error_prefix)


def optional_json_bool(
    payload: Mapping[str, Any],
    field: str,
    *,
    error_prefix: str = "JSON-001",
) -> bool | None:
    if field not in payload:
        return None
    return require_json_bool(payload, field, error_prefix=error_prefix)


def json_int(
    payload: Mapping[str, Any],
    field: str,
    *,
    default: Any = _MISSING,
    required: bool = False,
    minimum: int | None = None,
    maximum: int | None = None,
    error_prefix: str = "JSON-001",
) -> int:
    if field not in payload:
        if required or default is _MISSING:
            raise _message(error_prefix, field, "為必填欄位")
        return int(default)
    value = payload[field]
    if type(value) is not int:
        raise _message(error_prefix, field, "必須是整數")
    if minimum is not None and value < minimum:
        raise _message(error_prefix, field, f"不得小於 {minimum}")
    if maximum is not None and value > maximum:
        raise _message(error_prefix, field, f"不得大於 {maximum}")
    return value


def optional_json_int(
    payload: Mapping[str, Any],
    field: str,
    *,
    minimum: int,
    maximum: int,
    error_prefix: str = "JSON-001",
) -> int | None:
    if field not in payload:
        return None
    return json_int(
        payload,
        field,
        default=minimum,
        minimum=minimum,
        maximum=maximum,
        error_prefix=error_prefix,
    )


def nullable_json_int(
    payload: Mapping[str, Any],
    field: str,
    *,
    default: int | None = None,
    required: bool = False,
    minimum: int | None = None,
    maximum: int | None = None,
    error_prefix: str = "JSON-001",
) -> int | None:
    if field not in payload:
        if required:
            raise _message(error_prefix, field, "為必填欄位")
        return default
    if payload[field] is None:
        return None
    return json_int(
        payload,
        field,
        default=minimum,
        minimum=minimum,
        maximum=maximum,
        error_prefix=error_prefix,
    )


def optional_json_float(
    payload: Mapping[str, Any],
    field: str,
    *,
    minimum: float,
    maximum: float,
    error_prefix: str = "JSON-001",
) -> float | None:
    if field not in payload:
        return None
    value = payload[field]
    if type(value) not in {int, float}:
        raise _message(error_prefix, field, "必須是有限數字")
    resolved = float(value)
    if not math.isfinite(resolved):
        raise _message(error_prefix, field, "必須是有限數字")
    if not minimum <= resolved <= maximum:
        raise _message(error_prefix, field, f"必須介於 {minimum} 到 {maximum}")
    return resolved


def json_float(
    payload: Mapping[str, Any],
    field: str,
    *,
    default: Any = _MISSING,
    required: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
    error_prefix: str = "JSON-001",
) -> float:
    if field not in payload:
        if required or default is _MISSING:
            raise _message(error_prefix, field, "為必填欄位")
        value = default
    else:
        value = payload[field]
    if type(value) not in {int, float}:
        raise _message(error_prefix, field, "必須是有限數字")
    resolved = float(value)
    if not math.isfinite(resolved):
        raise _message(error_prefix, field, "必須是有限數字")
    if minimum is not None and resolved < minimum:
        raise _message(error_prefix, field, f"不得小於 {minimum}")
    if maximum is not None and resolved > maximum:
        raise _message(error_prefix, field, f"不得大於 {maximum}")
    return resolved


def nullable_json_float(
    payload: Mapping[str, Any],
    field: str,
    *,
    default: float | None = None,
    required: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
    error_prefix: str = "JSON-001",
) -> float | None:
    if field not in payload:
        if required:
            raise _message(error_prefix, field, "為必填欄位")
        return default
    if payload[field] is None:
        return None
    return json_float(
        payload,
        field,
        minimum=minimum,
        maximum=maximum,
        error_prefix=error_prefix,
    )


def json_string(
    payload: Mapping[str, Any],
    field: str,
    *,
    default: Any = _MISSING,
    required: bool = False,
    nullable: bool = False,
    minimum_length: int = 0,
    maximum_length: int | None = None,
    strip: bool = False,
    error_prefix: str = "JSON-001",
) -> str | None:
    if field not in payload:
        if required or default is _MISSING:
            raise _message(error_prefix, field, "為必填欄位")
        value = default
    else:
        value = payload[field]
    if value is None and nullable:
        return None
    if type(value) is not str:
        raise _message(error_prefix, field, "必須是字串")
    resolved = value.strip() if strip else value
    if len(resolved) < minimum_length:
        raise _message(error_prefix, field, f"長度不得小於 {minimum_length}")
    if maximum_length is not None and len(resolved) > maximum_length:
        raise _message(error_prefix, field, f"長度不得大於 {maximum_length}")
    return resolved


def reject_unknown_fields(
    payload: Mapping[str, Any],
    allowed_fields: Iterable[str],
    *,
    error_prefix: str,
) -> None:
    unknown = sorted(set(payload) - set(allowed_fields))
    if unknown:
        raise JsonScalarError(f"{error_prefix} 不支援欄位: {', '.join(unknown[:10])}")
