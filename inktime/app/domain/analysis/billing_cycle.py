"""Calendar billing periods, anchored to the user's renewal date (not UTC month 1)."""
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def billing_cycle(anchor: str, months: int, zone: str, now: datetime | None = None) -> dict[str, str]:
    renewal = date.fromisoformat(anchor)
    if not 1 <= months <= 12:
        raise ValueError("週期須為 1–12 個月")
    tz = ZoneInfo(zone)
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    origin = renewal.year * 12 + renewal.month - 1

    def boundary(offset):
        year, month = divmod(origin + offset * months, 12)
        month += 1
        return datetime(year, month, min(renewal.day, monthrange(year, month)[1]), tzinfo=tz)

    index = ((local.year * 12 + local.month - 1) - origin) // months
    if local < boundary(index):
        index -= 1
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "start": boundary(index).astimezone(timezone.utc).isoformat(),
        "end": boundary(index + 1).astimezone(timezone.utc).isoformat(),
        "day_start": day.astimezone(timezone.utc).isoformat(),
        "day_end": (day + timedelta(days=1)).astimezone(timezone.utc).isoformat(),
    }
