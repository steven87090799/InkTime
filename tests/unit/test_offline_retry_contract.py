from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from inktime.app.repositories.offline_schedules import OfflineScheduleRepository


def _retry(now_local: datetime, schedule: list[str]):
    return OfflineScheduleRepository.retry_after_details(
        now=now_local,
        timezone_name=str(now_local.tzinfo),
        schedule_times=schedule,
        prefetch_lead_minutes=5,
        server_margin_minutes=15,
    )


def test_retry_before_first_prepare_point_is_today_first_prepare_point():
    details = _retry(datetime(2026, 8, 3, 7, 39, tzinfo=ZoneInfo("Asia/Taipei")), ["08:00", "12:00"])
    assert datetime.fromtimestamp(details.retry_after_epoch, ZoneInfo("Asia/Taipei")).strftime("%H:%M") == "07:40"
    assert details.next_slot_epoch == int(datetime(2026, 8, 3, 8, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp())


def test_retry_after_first_prepare_point_stays_before_today_next_slot():
    details = _retry(datetime(2026, 8, 3, 7, 50, tzinfo=ZoneInfo("Asia/Taipei")), ["08:00", "12:00", "16:00", "20:00"])
    now_epoch = int(datetime(2026, 8, 3, 7, 50, tzinfo=ZoneInfo("Asia/Taipei")).timestamp())
    next_slot_epoch = int(datetime(2026, 8, 3, 8, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp())
    assert now_epoch < details.retry_after_epoch < next_slot_epoch
    assert datetime.fromtimestamp(details.retry_after_epoch, ZoneInfo("Asia/Taipei")).date().isoformat() == "2026-08-03"


def test_retry_at_midday_uses_today_fallback_not_tomorrow():
    details = _retry(datetime(2026, 8, 3, 10, 0, tzinfo=ZoneInfo("Asia/Taipei")), ["08:00", "12:00", "16:00"])
    now_epoch = int(datetime(2026, 8, 3, 10, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp())
    next_slot_epoch = int(datetime(2026, 8, 3, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp())
    assert now_epoch < details.retry_after_epoch < next_slot_epoch
    assert details.next_slot_epoch == next_slot_epoch


def test_retry_moves_to_tomorrow_only_after_all_today_slots_passed():
    details = _retry(datetime(2026, 8, 3, 20, 30, tzinfo=ZoneInfo("Asia/Taipei")), ["08:00", "20:00"])
    retry_local = datetime.fromtimestamp(details.retry_after_epoch, ZoneInfo("Asia/Taipei"))
    assert retry_local.date().isoformat() == "2026-08-04"
    assert retry_local.strftime("%H:%M") == "07:40"
    assert details.next_slot_epoch is None


def test_retry_uses_iana_dst_transition_and_keeps_epoch_bounds():
    zone = ZoneInfo("America/New_York")
    now = datetime(2026, 3, 8, 1, 30, tzinfo=zone)
    details = _retry(now, ["03:30", "12:00"])
    now_epoch = int(now.astimezone(timezone.utc).timestamp())
    next_slot_epoch = int(datetime(2026, 3, 8, 3, 30, tzinfo=zone).timestamp())
    assert now_epoch < details.retry_after_epoch < next_slot_epoch
    assert details.next_slot_epoch == next_slot_epoch
