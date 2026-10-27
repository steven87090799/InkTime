"""Resolve a device-day schedule without changing the frame wire contract."""
from datetime import date

from .offline_schedule import validate_offline_schedule


WEEKDAY_TIMES = "08:00,18:00,20:00,22:00"
WEEKEND_TIMES = "08:00,10:00,12:00,14:00,16:00,18:00,20:00,22:00"


def weekly_times(settings, target: date, fallback) -> list[str]:
    if not settings.get("display.weekly_schedule_enabled", True):
        return list(fallback)
    weekend = target.weekday() >= 5
    value = settings.get("display.weekend_times" if weekend else "display.weekday_times",
                         WEEKEND_TIMES if weekend else WEEKDAY_TIMES)
    return validate_offline_schedule(str(value).split(","), maximum=16, minimum_gap_minutes=30)
