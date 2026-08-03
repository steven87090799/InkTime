from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from inktime.app.domain.photopainter.offline_schedule import (
    next_sleep_epoch,
    prefetch_slots,
    slot_deadlines,
    validate_offline_schedule,
)
from inktime.app.repositories.offline_schedules import OfflineScheduleRepository


def test_offline_schedule_is_sorted_unique_and_has_five_minute_prefetch_slots():
    schedule = validate_offline_schedule(["20:00", "08:00", "12:00", "16:00"])

    assert schedule == ["08:00", "12:00", "16:00", "20:00"]
    assert prefetch_slots(schedule) == [
        ("08:00", "07:55"),
        ("12:00", "11:55"),
        ("16:00", "15:55"),
        ("20:00", "19:55"),
    ]


@pytest.mark.parametrize("value", [[], ["08:00", "08:00"], ["8:00"], ["24:00"]])
def test_offline_schedule_rejects_ambiguous_values(value):
    with pytest.raises(ValueError):
        validate_offline_schedule(value)


def test_next_sleep_epoch_uses_exact_local_slot():
    now = datetime(2026, 8, 2, 7, 56, tzinfo=timezone.utc)

    next_epoch = next_sleep_epoch(
        now=now,
        schedule=["08:00", "12:00"],
        timezone_name="UTC",
        lead_minutes=0,
    )

    assert next_epoch == int(datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc).timestamp())


def test_slot_deadlines_keep_prefetched_items_until_the_following_slot():
    deadlines = slot_deadlines(
        date(2026, 8, 3),
        ["08:00", "20:00"],
        "Asia/Taipei",
        grace_minutes=15,
    )

    assert deadlines == [
        "2026-08-03T12:15:00+00:00",
        "2026-08-04T00:15:00+00:00",
    ]


@pytest.mark.parametrize(
    ("target_date", "expected_epoch"),
    [
        (date(2026, 1, 15), int(datetime(2026, 1, 15, 13, tzinfo=timezone.utc).timestamp())),
        (date(2026, 7, 15), int(datetime(2026, 7, 15, 12, tzinfo=timezone.utc).timestamp())),
    ],
)
def test_server_show_at_epoch_is_iana_timezone_authoritative(target_date, expected_epoch):
    show_at = OfflineScheduleRepository._show_at(target_date, "08:00", "America/New_York")

    assert int(datetime.fromisoformat(show_at).timestamp()) == expected_epoch
