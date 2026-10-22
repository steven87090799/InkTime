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
