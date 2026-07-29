from __future__ import annotations

import math
from typing import Any, Mapping


class JsonScalarError(ValueError):
    pass


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
    default: bool,
    error_prefix: str = "JSON-001",
) -> bool:
    if field not in payload:
        return default
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
    default: int,
    minimum: int,
    maximum: int,
    error_prefix: str = "JSON-001",
) -> int:
    if field not in payload:
        return default
    value = payload[field]
    if type(value) is not int:
        raise _message(error_prefix, field, "必須是整數")
    if not minimum <= value <= maximum:
        raise _message(error_prefix, field, f"必須介於 {minimum} 到 {maximum}")
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
    default: int | None,
    minimum: int,
    maximum: int,
    error_prefix: str = "JSON-001",
) -> int | None:
    if field not in payload:
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
