"""Prefer unused photos even when a legacy frame cannot acknowledge display."""

from __future__ import annotations

from typing import Any


def exposure_count(photo: dict[str, Any]) -> int:
    return max(int(photo.get("display_count") or 0), int(photo.get("selection_count") or 0),
               int(photo.get("exposure_count") or 0))


def last_exposure_at(photo: dict[str, Any]) -> str:
    return max(str(photo.get("last_displayed_at") or ""), str(photo.get("last_selected_at") or ""),
               str(photo.get("content_last_exposure_at") or ""))
