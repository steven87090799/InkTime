"""Bounded local parsing for model text responses.

This helper never receives an image and never calls a provider.  It only
extracts one unambiguous JSON object from a response so a harmless wrapper
such as ``Here is the JSON: {...}`` does not consume the one permitted
text-only repair request.
"""

from __future__ import annotations

import ast
from copy import deepcopy
import json
from typing import Any

from inktime.app.domain.analysis.schema import AnalysisValidationError


SEMANTIC_INTEGRITY_FIELDS = frozenset(
    {
        "memory_score",
        "visual_score",
        "special_level",
        "special_codes",
        "people_count",
        "content_filter",
        "visual_orientation",
        "subject_position",
        "text_safe_area",
    }
)


def _fenced_object(text: str) -> dict[str, Any] | None:
    """Accept exactly one object in a Markdown fence, with an optional label."""

    lines = text.splitlines()
    if len(lines) < 3 or not lines[0].strip().startswith("```") or lines[-1].strip() != "```":
        return None
    body = "\n".join(lines[1:-1]).strip()
    if body.startswith("json\n") or body.startswith("json\r\n"):
        body = body.partition("\n")[2].strip()
    try:
        value, end = json.JSONDecoder().raw_decode(body)
    except json.JSONDecodeError:
        return None
    return value if candidate_is_object(value, body[end:]) else None


def candidate_is_object(value: Any, trailing: str = "") -> bool:
    return isinstance(value, dict) and not trailing.strip()


def _wrapped_object(text: str) -> dict[str, Any] | None:
    """Find one complete outer JSON object in otherwise non-JSON prose.

    Nested objects are part of the outer candidate and are not counted as
    separate responses.  Complete arrays are deliberately counted and reject
    the response, even when they contain only one object.
    """

    decoder = json.JSONDecoder()
    candidates: list[tuple[int, int, Any]] = []
    index = 0
    while index < len(text):
        marker = text[index]
        if marker not in "[{":
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            index += 1
            continue
        absolute_end = index + end
        if isinstance(value, (dict, list)):
            candidates.append((index, absolute_end, value))
            # Skip nested braces/brackets belonging to this complete value.
            index = absolute_end
        else:
            index += 1
    if len(candidates) != 1:
        return None
    _start, _end, value = candidates[0]
    return value if isinstance(value, dict) else None


def extract_json_value(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(text)
    except json.JSONDecodeError:
        value = None
        end = 0
    if candidate_is_object(value, text[end:]):
        return value
    if text.startswith("```"):
        return _fenced_object(text)
    return _wrapped_object(text)


def repair_source_object(raw: str) -> dict[str, Any] | None:
    """Return only a safely recoverable source object for JSON repair.

    Strict JSON, Markdown fences and wrappers are handled first. Python's
    literal parser is a bounded fallback for unambiguous syntax differences
    such as single quotes or a trailing comma; it cannot execute code.
    """

    extracted = extract_json_value(raw)
    if extracted is not None:
        return extracted
    text = str(raw or "").strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    try:
        candidate = ast.literal_eval(text)
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        return None
    return candidate if isinstance(candidate, dict) else None


def semantic_repair_snapshot(source: dict[str, Any] | None) -> dict[str, Any]:
    """Require all photo semantics before allowing text-only repair."""

    missing = sorted(SEMANTIC_INTEGRITY_FIELDS - set(source or {}))
    if source is None or missing:
        error = AnalysisValidationError(
            "JSON 回應缺少可驗證的照片語意欄位，必須重新執行 Vision Analysis"
            + (f"：{', '.join(missing)}" if missing else "")
        )
        error.code = "VLM-007"
        raise error
    return {field: deepcopy(source[field]) for field in SEMANTIC_INTEGRITY_FIELDS}


def assert_semantic_repair_integrity(
    snapshot: dict[str, Any], repaired: dict[str, Any]
) -> None:
    """Reject a repair response that added or changed photo semantics."""

    changed = sorted(
        field for field, original in snapshot.items() if repaired.get(field) != original
    )
    if changed:
        error = AnalysisValidationError(
            "JSON Repair 修改了既有照片語意，必須重新執行 Vision Analysis："
            + ", ".join(changed)
        )
        error.code = "VLM-007"
        raise error
