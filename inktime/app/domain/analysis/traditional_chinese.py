from __future__ import annotations

from typing import Any

from opencc import OpenCC


_TAIWAN_TRADITIONAL_CONVERTER = OpenCC("s2twp.json")


def to_taiwan_traditional(value: Any) -> Any:
    """Convert natural-language values to Taiwan Traditional Chinese.

    Dictionary keys are protocol identifiers, so they must remain unchanged.
    The recursive conversion covers captions, reasons, semantic details, and
    other model-provided strings before they reach validation or persistence.
    """

    if isinstance(value, str):
        return _TAIWAN_TRADITIONAL_CONVERTER.convert(value)
    if isinstance(value, list):
        return [to_taiwan_traditional(item) for item in value]
    if isinstance(value, dict):
        return {key: to_taiwan_traditional(item) for key, item in value.items()}
    return value
