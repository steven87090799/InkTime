from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ANCHOR_YEAR = 2000  # 固定閏年，完整涵蓋 02-29。


def current_local_date(timezone_name: str = "Asia/Taipei", now_utc: datetime | None = None) -> date:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"不支援的時區：{timezone_name}") from exc
    current = now_utc or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(zone).date()


def month_day_to_day_of_year(month_day: str) -> int:
    try:
        value = datetime.strptime(f"{ANCHOR_YEAR}-{month_day}", "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"無效月日：{month_day}") from exc
    return (value - date(ANCHOR_YEAR, 1, 1)).days + 1


def day_of_year_to_month_day(day: int) -> str:
    normalized = ((day - 1) % 366) + 1
    value = date.fromordinal(date(ANCHOR_YEAR, 1, 1).toordinal() + normalized - 1)
    return value.strftime("%m-%d")
