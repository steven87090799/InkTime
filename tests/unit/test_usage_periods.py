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


def test_photo_limits_reset_at_local_midnight_and_month_boundary(app):
    from inktime.app.repositories.usage import UsageRepository
    from tests.integration.test_jobs import add_photos

    photos = add_photos(app, 2)
    usage = UsageRepository(app.extensions["inktime_database"])
    values = dict(provider="fixture", model="test", job_id=None, request_type="single",
                  input_tokens=10, output_tokens=2, cached_tokens=0, estimated_cost=0.01,
                  actual_cost=None, latency_ms=1, status="completed")
    periods = usage_periods("Asia/Taipei", now=datetime.fromisoformat("2026-08-31T16:00:01+00:00"))
    usage.record(photo_id=photos[0], started_at="2026-08-31T15:59:59+00:00", **values)
    repository = app.extensions["inktime_photo_repository"]
    assert not repository.ai_limit_reached(daily_limit=1, monthly_limit=1, periods=periods)
    usage.record(photo_id=photos[1], started_at="2026-08-31T16:00:01+00:00", **values)
    assert repository.ai_limit_reached(daily_limit=1, monthly_limit=100, periods=periods)
    assert repository.ai_limit_reached(daily_limit=100, monthly_limit=1, periods=periods)
