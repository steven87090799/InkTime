"""Deterministic server-side rules for optional PhotoPainter offline slots."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import re
from zoneinfo import ZoneInfo


_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_DELIVERY_MODES = {"legacy_online", "stock_compat", "inktime_offline_schedule"}
_ENHANCED_DELIVERY_MODE = "inktime_offline_schedule"


def validate_offline_schedule(values, *, maximum: int = 24) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= maximum:
        raise ValueError("DEVICE-008 offline_schedule 必須包含 1 到 24 個時刻")
    normalized = [str(value).strip() for value in values]
    if any(_TIME.fullmatch(value) is None for value in normalized):
        raise ValueError("DEVICE-008 offline_schedule 必須使用 HH:MM")
    if len(set(normalized)) != len(normalized):
        raise ValueError("DEVICE-008 offline_schedule 不可重複")
    return sorted(normalized)


def normalize_delivery_contract(
    delivery_mode: str,
    offline_prefetch_allowed: bool | None = None,
    *,
    explicit_prefetch: bool = False,
) -> tuple[str, bool]:
    """Return one safe delivery-mode/prefetch pair.

    An omitted prefetch flag is deliberately normalized from the selected
    delivery mode.  A caller that explicitly supplies a contradictory flag
    fails closed so API, repository, and database boundaries cannot drift.
    """

    mode = str(delivery_mode).strip()
    if mode not in _DELIVERY_MODES:
        raise ValueError("DEVICE-008 delivery_mode 不合法")
    expected = mode == _ENHANCED_DELIVERY_MODE
    if not explicit_prefetch or offline_prefetch_allowed is None:
        return mode, expected
    if type(offline_prefetch_allowed) is not bool:
        raise ValueError("DEVICE-008 offline_prefetch_allowed 必須是 Boolean")
    if offline_prefetch_allowed != expected:
        raise ValueError("DEVICE-008 delivery_mode 與 offline_prefetch_allowed 不一致")
    return mode, expected


def schedule_minutes(value: str) -> int:
    if _TIME.fullmatch(str(value)) is None:
        raise ValueError("DEVICE-008 時刻格式錯誤")
    hour, minute = (int(part) for part in str(value).split(":"))
    return hour * 60 + minute


def prefetch_slots(values: list[str], *, lead_minutes: int = 5) -> list[tuple[str, str]]:
    schedule = validate_offline_schedule(values)
    if type(lead_minutes) is not int or not 0 <= lead_minutes <= 120:
        raise ValueError("DEVICE-008 prefetch 提前分鐘必須介於 0 到 120")
    result = []
    for slot in schedule:
        minutes = schedule_minutes(slot) - int(lead_minutes)
        if minutes < 0:
            minutes += 24 * 60
        result.append((slot, f"{minutes // 60:02d}:{minutes % 60:02d}"))
    return result


def slot_deadlines(
    target_date: date,
    values: list[str],
    timezone_name: str,
    *,
    grace_minutes: int = 15,
) -> list[str]:
    """Return UTC deadlines after the following slot, not after this slot.

    A prefetched image may be downloaded well before its display time.  Its
    queue reservation therefore remains valid until the next scheduled slot
    (plus a small grace period); expiring it immediately after its own display
    time would discard a still-valid offline queue item.
    """

    if type(grace_minutes) is not int or not 0 <= grace_minutes <= 1440:
        raise ValueError("DEVICE-008 slot deadline 寬限分鐘必須介於 0 到 1440")
    schedule = validate_offline_schedule(values, maximum=12)
    zone = ZoneInfo(timezone_name)
    starts: list[datetime] = []
    for slot in schedule:
        hour, minute = (int(part) for part in slot.split(":"))
        starts.append(datetime.combine(target_date, time(hour, minute), tzinfo=zone))
    first_next_day = datetime.combine(
        target_date + timedelta(days=1),
        time(starts[0].hour, starts[0].minute),
        tzinfo=zone,
    )
    following = starts[1:] + [first_next_day]
    return [
        (next_start + timedelta(minutes=grace_minutes)).astimezone(ZoneInfo("UTC")).isoformat()
        for next_start in following
    ]


def next_sleep_epoch(*, now: datetime, schedule: list[str], timezone_name: str, lead_minutes: int = 0) -> int:
    """Return the next exact local-slot epoch; never use a relative 24h timer."""
    from zoneinfo import ZoneInfo

    if type(lead_minutes) is not int or not 0 <= lead_minutes <= 120:
        raise ValueError("DEVICE-008 prefetch 提前分鐘必須介於 0 到 120")
    local = now.astimezone(ZoneInfo(timezone_name))
    slots = validate_offline_schedule(schedule)
    candidates: list[datetime] = []
    for day_offset in (0, 1):
        target_date = local.date() + timedelta(days=day_offset)
        for slot in slots:
            hour, minute = (int(part) for part in slot.split(":"))
            candidate = datetime.combine(target_date, time(hour, minute), tzinfo=local.tzinfo)
            if candidate > local:
                candidates.append(candidate - timedelta(minutes=int(lead_minutes)))
    if not candidates:
        raise ValueError("DEVICE-008 找不到下一個離線時刻")
    return int(min(candidates).timestamp())
