"""UTC query bounds for the installation's local billing calendar."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def usage_periods(timezone_name: str, *, now: datetime | None = None) -> dict[str, str]:
    zone = ZoneInfo(timezone_name)
    instant = now or datetime.now(timezone.utc)
    today = instant.astimezone(zone).date()
    month = today.replace(day=1)
    next_month = (month + timedelta(days=32)).replace(day=1)

    def midnight(day):
        return datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc).isoformat()

    return {
        "day_start": midnight(today),
        "day_end": midnight(today + timedelta(days=1)),
        "month_start": midnight(month),
        "month_end": midnight(next_month),
        "week_start": (instant - timedelta(days=7)).astimezone(timezone.utc).isoformat(),
        "now": instant.astimezone(timezone.utc).isoformat(),
    }
