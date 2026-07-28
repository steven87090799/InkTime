"""Deterministic, non-generative captions for local-only rendering."""
from __future__ import annotations

from datetime import date, datetime, timezone as utc_timezone
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


LOCAL_CAPTION_VERSION = "local-caption-v1"


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or "UTC"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _as_date(value: Any, *, timezone_name: str) -> date | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # Existing photo timestamps without an offset are treated as UTC,
            # matching parse_photo_date's explicit server-neutral contract.
            value = value.replace(tzinfo=utc_timezone.utc)
        return value.astimezone(_zone(timezone_name)).date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        if "T" not in str(value) and " " not in str(value):
            return date.fromisoformat(str(value))
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=utc_timezone.utc)
        return parsed.astimezone(_zone(timezone_name)).date()
    except ValueError:
        return None


def _shorten(text: str, maximum: int) -> str:
    text = text.strip()
    if maximum < 1:
        maximum = 1
    if len(text) <= maximum:
        return text
    return text[:maximum].rstrip("・，、 ") or text[:maximum]


def build_local_caption(
    *,
    photo_id: str,
    captured_at: Any,
    display_date: Any,
    timezone: str,
    known_location: str | None = None,
    years_ago: int | None = None,
    selection_mode: str = "local_only",
    manual_caption: str | None = None,
    existing_side_caption: str | None = None,
    existing_caption_source: str | None = None,
    existing_caption_is_ai_generated: bool | None = None,
    source_detail: dict[str, Any] | None = None,
    source_updated_at: str | None = None,
    orientation: str | None = None,
    maximum_characters: int = 16,
) -> dict[str, Any]:
    """Resolve one photo's text without inferring people, events, or emotion."""
    _ = (selection_mode, orientation)  # part of the explicit stable contract
    manual = str(manual_caption or "").strip()
    existing = str(existing_side_caption or "").strip()
    captured, display = (
        _as_date(captured_at, timezone_name=timezone),
        _as_date(display_date, timezone_name=timezone),
    )
    location = str(known_location or "").strip()
    source = "local_fallback"
    if manual:
        text, source, ai = manual, "manual_caption", False
    elif existing:
        text = existing
        source = str(existing_caption_source or "existing_caption_unknown")
        ai = bool(existing_caption_is_ai_generated) if existing_caption_is_ai_generated is not None else False
    elif captured and display and (captured.month, captured.day) == (display.month, display.day):
        elapsed = years_ago if years_ago is not None else max(0, display.year - captured.year)
        text, source, ai = (
            (f"{elapsed}年前的今天" if elapsed else "那年今日"),
            "local_historical_today",
            False,
        )
    elif location and captured:
        text, source, ai = f"{captured.year}年・{location}", "local_location_year", False
    elif captured:
        text, source, ai = f"{captured.year}年{captured.month}月{captured.day}日", "local_capture_date", False
    else:
        text, ai = "那年今日", False
    text = _shorten(text, maximum_characters)
    return {
        "text": text or "那年今日",
        "source": source,
        "version": LOCAL_CAPTION_VERSION,
        "photo_id": str(photo_id),
        "text_hash": sha256((text or "那年今日").encode("utf-8")).hexdigest(),
        "is_ai_generated": ai,
        "source_detail": dict(source_detail or {}) if ai else {},
        "source_updated_at": source_updated_at if existing else None,
    }
