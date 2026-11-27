"""A recurring calendar window, including year boundaries and leap day.

Use a leap-year calendar so February 29 has its own slot every year. A
non-leap-year February 28 window of at least one day includes February 29.
"""

from datetime import date, timedelta


def seasonal_month_days(target: date, window_days: int) -> list[str]:
    if type(window_days) is not int or not 0 <= window_days <= 31:
        raise ValueError("同期日期範圍必須介於前後 0 至 31 天")
    anchor = date(2000, target.month, target.day)
    return sorted({
        (anchor + timedelta(days=offset)).strftime("%m-%d")
        for offset in range(-window_days, window_days + 1)
    })


def upcoming_display_priorities(today: date, nearby_days: int) -> dict[str, int]:
    """Eight actual display dates first, then their recurring calendar fallbacks."""
    targets = [today + timedelta(days=offset) for offset in range(8)]
    priorities = {target.strftime("%m-%d"): offset for offset, target in enumerate(targets)}
    for distance in range(1, nearby_days + 1):
        for target in targets:
            anchor = date(2000, target.month, target.day)
            for offset in (-distance, distance):
                priorities.setdefault((anchor + timedelta(days=offset)).strftime("%m-%d"), 7 + distance)
    return priorities
