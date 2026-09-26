from __future__ import annotations

from typing import Any, Collection

from opencc import OpenCC


_TAIWAN_TRADITIONAL_CONVERTER = OpenCC("s2twp.json")


def to_taiwan_traditional(value: Any, *, protected: Collection[str] = ()) -> Any:
    """Convert natural-language values to Taiwan Traditional Chinese.

    Dictionary keys are protocol identifiers, so they must remain unchanged.
    The recursive conversion covers captions, reasons, semantic details, and
    other model-provided strings before they reach validation or persistence.

    ``protected`` holds protocol *values* that must survive verbatim.  s2twp
    applies Taiwanese vocabulary substitution, not just character conversion, so
    an enum member such as ``文件`` would otherwise be rewritten to ``檔案`` and
    then fail its own enum check.  Enum members are identifiers, not prose, and
    must be excluded from conversion exactly like dictionary keys are.
    """

    guarded = frozenset(protected)
    if isinstance(value, str):
        if value in guarded:
            return value
        return _TAIWAN_TRADITIONAL_CONVERTER.convert(value)
    if isinstance(value, list):
        return [to_taiwan_traditional(item, protected=guarded) for item in value]
    if isinstance(value, dict):
        return {key: to_taiwan_traditional(item, protected=guarded) for key, item in value.items()}
    return value
