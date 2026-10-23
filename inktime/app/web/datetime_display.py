"""Human-readable UI dates; storage and protocol timestamps remain unchanged."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def display_timezone(value: object) -> str:
    name = str(value or "Asia/Taipei")
    try:
        return ZoneInfo(name).key
    except (ValueError, ZoneInfoNotFoundError):
        return "Asia/Taipei"


def format_display_datetime(value: object, timezone_name: str = "Asia/Taipei", *, naive_is_utc: bool = True) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.strftime("%Y/%m/%d")
    raw = str(value).strip()
    try:
        if len(raw) == 10:
            return date.fromisoformat(raw).strftime("%Y/%m/%d")
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(raw.replace("Z", "+00:00"))
        # SQLite system timestamps without an offset are UTC. EXIF dates use
        # the explicit photo filter to preserve an unknown camera timezone.
        if parsed.tzinfo is None and naive_is_utc:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(ZoneInfo(display_timezone(timezone_name)))
        return parsed.strftime("%Y/%m/%d %H:%M:%S")
    except (ValueError, TypeError, OverflowError):
        return raw
