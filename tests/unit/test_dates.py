from __future__ import annotations

from datetime import datetime, timezone

import pytest

from inktime.app.domain.rendering.dates import (
    current_local_date,
    day_of_year_to_month_day,
    month_day_to_day_of_year,
)


@pytest.mark.parametrize("month_day", ["02-28", "02-29", "03-01", "12-31", "01-01"])
def test_leap_anchor_round_trip(month_day):
    assert day_of_year_to_month_day(month_day_to_day_of_year(month_day)) == month_day


def test_utc_and_taipei_can_be_different_dates():
    instant = datetime(2026, 7, 16, 16, 30, tzinfo=timezone.utc)
    assert current_local_date("UTC", instant).isoformat() == "2026-07-16"
    assert current_local_date("Asia/Taipei", instant).isoformat() == "2026-07-17"


def test_daylight_saving_timezone_uses_zoneinfo():
    instant = datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)
    assert current_local_date("America/New_York", instant).isoformat() == "2026-03-08"


def test_invalid_month_day_is_rejected():
    with pytest.raises(ValueError):
        month_day_to_day_of_year("02-30")
