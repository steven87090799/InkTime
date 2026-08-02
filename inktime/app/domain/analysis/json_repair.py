"""Bounded local parsing for model text responses.

This helper never receives an image and never calls a provider.  It only
extracts one JSON object/array from a response so a harmless wrapper such as
``Here is the JSON: {...}`` does not consume the one permitted text-only
repair request.
"""

from __future__ import annotations

import json
from typing import Any


def extract_json_value(raw: str) -> dict[str, Any] | list[Any] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    candidates = [text]
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        candidates.append("\n".join(lines[1:-1]).strip())
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            value, end = decoder.raw_decode(candidate)
            if candidate[end:].strip() == "" and isinstance(value, (dict, list)):
                return value
        except json.JSONDecodeError:
            pass
        for index, marker in enumerate(candidate):
            if marker not in "[{":
                continue
            try:
                value, end = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, (dict, list)):
                return value
    return None
