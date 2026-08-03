"""Bounded local parsing for model text responses.

This helper never receives an image and never calls a provider.  It only
extracts one unambiguous JSON object from a response so a harmless wrapper
such as ``Here is the JSON: {...}`` does not consume the one permitted
text-only repair request.
"""

from __future__ import annotations

import json
from typing import Any


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
