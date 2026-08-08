from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from inktime.app.domain.photopainter.offline_schedule import (
    LEGACY_MAX_OFFLINE_SLOTS,
    MAX_OFFLINE_SLOTS,
    normalize_sync_strategy,
    next_sync_epoch,
    next_sleep_epoch,
    offline_schedule_capability_is_usable,
    offline_schedule_capability_state,
    prefetch_slots,
    resolve_offline_schedule_max_slots,
    slot_deadlines,
    validate_offline_schedule,
)
from inktime.app.repositories.offline_schedules import OfflineScheduleRepository


@pytest.mark.parametrize(
    ("capabilities", "expected"),
    [
        ({}, LEGACY_MAX_OFFLINE_SLOTS),
        ({"offline_schedule_max_slots": 12}, LEGACY_MAX_OFFLINE_SLOTS),
        ({"offline_schedule_max_slots": 24}, MAX_OFFLINE_SLOTS),
        ({"offline_schedule_max_slots": 13}, LEGACY_MAX_OFFLINE_SLOTS),
        ({"offline_schedule_max_slots": "24"}, LEGACY_MAX_OFFLINE_SLOTS),
    ],
)
def test_offline_schedule_capability_resolver_defaults_unknown_to_legacy(capabilities, expected):
    assert resolve_offline_schedule_max_slots(capabilities) == expected


def test_offline_schedule_capability_state_distinguishes_safe_unknown_and_explicit_24():
    assert offline_schedule_capability_state(12) == "unknown_12"
    assert offline_schedule_capability_state(24) == "confirmed_24"
    assert offline_schedule_capability_is_usable("unknown_12")
    assert offline_schedule_capability_is_usable("confirmed_24")
    assert not offline_schedule_capability_is_usable("legacy_ambiguous")


def test_offline_schedule_capability_boundary_rejects_legacy_13th_slot():
    schedule = [f"{hour:02d}:00" for hour in range(13)]
    with pytest.raises(ValueError, match="1 到 12"):
        validate_offline_schedule(schedule, maximum=LEGACY_MAX_OFFLINE_SLOTS)
    assert len(validate_offline_schedule(schedule, maximum=MAX_OFFLINE_SLOTS)) == 13


def test_device_capability_limits_deadlines_and_next_sync():
    schedule = [f"{hour:02d}:00" for hour in range(13)]
    with pytest.raises(ValueError, match="1 到 12"):
        slot_deadlines(
            date(2026, 8, 3),
            schedule,
            "Asia/Taipei",
            maximum_slots=LEGACY_MAX_OFFLINE_SLOTS,
        )
    assert slot_deadlines(
        date(2026, 8, 3),
        schedule,
        "Asia/Taipei",
        maximum_slots=MAX_OFFLINE_SLOTS,
    )
    with pytest.raises(ValueError, match="1 到 12"):
        next_sync_epoch(
            now=datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc),
            schedule=schedule,
            timezone_name="Asia/Taipei",
            maximum_slots=LEGACY_MAX_OFFLINE_SLOTS,
        )


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


def test_offline_schedule_enforces_circular_minimum_gap_with_bounded_override():
    with pytest.raises(ValueError, match="循環最小間隔"):
        validate_offline_schedule(["23:30", "00:00"])
    assert validate_offline_schedule(
        ["23:30", "00:00"], minimum_gap_minutes=30
    ) == ["00:00", "23:30"]


def test_fixed_daily_sync_strategy_controls_next_sleep_epoch():
    now = datetime(2026, 8, 2, 7, 0, tzinfo=timezone.utc)
    next_epoch = next_sleep_epoch(
        now=now,
        schedule=["08:00", "20:00"],
        timezone_name="UTC",
        lead_minutes=0,
        sync_strategy="fixed_daily",
        sync_time="07:30",
    )
    assert next_epoch == int(datetime(2026, 8, 2, 7, 30, tzinfo=timezone.utc).timestamp())
    with pytest.raises(ValueError, match="不接受 sync_time"):
        normalize_sync_strategy("first_display_lead", "07:30")


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


def test_next_prepare_deadline_is_bounded_and_skips_committed_target():
    zone = ZoneInfo("Asia/Taipei")
    before_first = OfflineScheduleRepository.next_prepare_deadline(
        now=datetime(2026, 8, 3, 7, 0, tzinfo=zone),
        timezone_name="Asia/Taipei",
        schedule_times=["08:00", "20:00"],
        prefetch_lead_minutes=5,
        server_margin_minutes=15,
        future_prepare_hour_local=20,
    )
    assert before_first == "2026-08-02T23:40:00+00:00"

    after_prepare = OfflineScheduleRepository.next_prepare_deadline(
        now=datetime(2026, 8, 3, 10, 0, tzinfo=zone),
        timezone_name="Asia/Taipei",
        schedule_times=["08:00", "20:00"],
        prefetch_lead_minutes=5,
        server_margin_minutes=15,
        future_prepare_hour_local=20,
        skip_target_dates=["2026-08-03"],
    )
    assert after_prepare == "2026-08-03T23:40:00+00:00"

    # At 21:00 the late slot today and the configured future handoff for
    # tomorrow are both due.  Skipping today's committed target must leave
    # tomorrow immediately due instead of pushing it into the future.
    both_due = OfflineScheduleRepository.next_prepare_deadline(
        now=datetime(2026, 8, 3, 21, 0, tzinfo=zone),
        timezone_name="Asia/Taipei",
        schedule_times=["08:00", "22:00"],
        prefetch_lead_minutes=5,
        server_margin_minutes=15,
        future_prepare_hour_local=20,
        skip_target_dates=["2026-08-03"],
    )
    assert both_due == "2026-08-03T13:00:00+00:00"
