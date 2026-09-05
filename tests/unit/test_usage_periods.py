from datetime import datetime

import pytest

from inktime.app.services.usage_periods import usage_periods


@pytest.mark.parametrize(
    "zone,instant,start,end,month_start,month_end",
    [
        ("Asia/Taipei", "2026-08-31T16:00:00+00:00", "2026-08-31T16:00:00+00:00",
         "2026-09-01T16:00:00+00:00", "2026-08-31T16:00:00+00:00", "2026-09-30T16:00:00+00:00"),
        ("America/New_York", "2026-03-08T12:00:00+00:00", "2026-03-08T05:00:00+00:00",
         "2026-03-09T04:00:00+00:00", "2026-03-01T05:00:00+00:00", "2026-04-01T04:00:00+00:00"),
        ("America/New_York", "2026-11-01T12:00:00+00:00", "2026-11-01T04:00:00+00:00",
         "2026-11-02T05:00:00+00:00", "2026-11-01T04:00:00+00:00", "2026-12-01T05:00:00+00:00"),
        ("Asia/Taipei", "2026-12-31T16:00:00+00:00", "2026-12-31T16:00:00+00:00",
         "2027-01-01T16:00:00+00:00", "2026-12-31T16:00:00+00:00", "2027-01-31T16:00:00+00:00"),
    ],
)
def test_local_calendar_bounds(zone, instant, start, end, month_start, month_end):
    periods = usage_periods(zone, now=datetime.fromisoformat(instant))
    assert (periods["day_start"], periods["day_end"]) == (start, end)
    assert (periods["month_start"], periods["month_end"]) == (month_start, month_end)
