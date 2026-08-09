"""Deterministic server-side rules for optional PhotoPainter offline slots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
import re
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_DELIVERY_MODES = {"legacy_online", "stock_compat", "inktime_offline_schedule"}
_ENHANCED_DELIVERY_MODE = "inktime_offline_schedule"
_SYNC_STRATEGIES = {"first_display_lead", "fixed_daily"}
MINIMUM_SCHEDULE_GAP_MINUTES = 60
MINIMUM_ALLOWED_GAP_MINUTES = 30
MAXIMUM_ALLOWED_GAP_MINUTES = 360
LEGACY_MAX_OFFLINE_SLOTS = 12
MAX_OFFLINE_SLOTS = 24
OFFLINE_CAPABILITY_UNKNOWN_12 = "unknown_12"
OFFLINE_CAPABILITY_CONFIRMED_24 = "confirmed_24"
OFFLINE_CAPABILITY_LEGACY_AMBIGUOUS = "legacy_ambiguous"
OFFLINE_CAPABILITY_USABLE_STATES = frozenset(
    {OFFLINE_CAPABILITY_UNKNOWN_12, OFFLINE_CAPABILITY_CONFIRMED_24}
)
OFFLINE_PREPARE_BOOTSTRAP_AT = "1970-01-01T00:00:00+00:00"


@dataclass(frozen=True)
class StoredScheduleState:
    """Lossless view of a stored schedule used by quarantine remediation."""

    raw: str
    is_array: bool
    values: list[Any]


@dataclass(frozen=True)
class OfflinePreparePlan:
    """Pure, timezone-aware decision shared by Scheduler and persistence."""

    due_target_dates: tuple[date, ...]
    next_deadline: datetime


def stored_schedule_state(value: object) -> StoredScheduleState:
    """Decode a schedule without destroying malformed or non-array history."""

    raw = str(value) if value is not None else "[]"
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return StoredScheduleState(raw=raw, is_array=False, values=[])
    if not isinstance(parsed, list):
        return StoredScheduleState(raw=raw, is_array=False, values=[])
    return StoredScheduleState(raw=raw, is_array=True, values=list(parsed))


def resolve_offline_schedule_max_slots(capabilities: Mapping[str, Any] | None) -> int:
    """Resolve the device capability without granting unknown firmware 24 slots."""

    if not isinstance(capabilities, Mapping):
        return LEGACY_MAX_OFFLINE_SLOTS
    value = capabilities.get("offline_schedule_max_slots")
    if type(value) is int and value == MAX_OFFLINE_SLOTS:
        return MAX_OFFLINE_SLOTS
    return LEGACY_MAX_OFFLINE_SLOTS


def offline_schedule_capability_state(maximum_slots: int) -> str:
    """Return the persisted state for a safely resolved numeric capability."""

    return (
        OFFLINE_CAPABILITY_CONFIRMED_24
        if resolve_offline_schedule_max_slots({"offline_schedule_max_slots": maximum_slots})
        == MAX_OFFLINE_SLOTS
        else OFFLINE_CAPABILITY_UNKNOWN_12
    )


def offline_schedule_capability_is_usable(state: Any) -> bool:
    """Allow only known-safe states to stage or deliver offline playlists."""

    return str(state or "") in OFFLINE_CAPABILITY_USABLE_STATES


def validate_offline_schedule(
    values,
    *,
    maximum: int = MAX_OFFLINE_SLOTS,
    minimum_gap_minutes: int = MINIMUM_SCHEDULE_GAP_MINUTES,
) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= maximum:
        raise ValueError(f"DEVICE-008 offline_schedule 必須包含 1 到 {maximum} 個時刻")
    if type(minimum_gap_minutes) is not int or not MINIMUM_ALLOWED_GAP_MINUTES <= minimum_gap_minutes <= MAXIMUM_ALLOWED_GAP_MINUTES:
        raise ValueError("DEVICE-008 minimum_schedule_gap_minutes 必須介於 30 到 360")
    normalized = [str(value).strip() for value in values]
    if any(_TIME.fullmatch(value) is None for value in normalized):
        raise ValueError("DEVICE-008 offline_schedule 必須使用 HH:MM")
    if len(set(normalized)) != len(normalized):
        raise ValueError("DEVICE-008 offline_schedule 不可重複")
    ordered = sorted(normalized)
    minutes = [schedule_minutes(value) for value in ordered]
    gaps = [right - left for left, right in zip(minutes, minutes[1:], strict=False)]
    if len(minutes) > 1:
        gaps.append(minutes[0] + 24 * 60 - minutes[-1])
    if any(gap < minimum_gap_minutes for gap in gaps):
        raise ValueError(
            "DEVICE-008 schedule_times 的循環最小間隔不得小於 "
            f"{minimum_gap_minutes} 分鐘"
        )
    return ordered


def normalize_sync_strategy(
    sync_strategy: str = "first_display_lead", sync_time: str | None = None
) -> tuple[str, str | None]:
    strategy = str(sync_strategy or "first_display_lead").strip()
    if strategy not in _SYNC_STRATEGIES:
        raise ValueError("DEVICE-008 sync_strategy 不合法")
    normalized_time = None if sync_time in (None, "") else str(sync_time).strip()
    if strategy == "first_display_lead":
        if normalized_time is not None:
            raise ValueError("DEVICE-008 first_display_lead 不接受 sync_time")
        return strategy, None
    if normalized_time is None or _TIME.fullmatch(normalized_time) is None:
        raise ValueError("DEVICE-008 fixed_daily 必須提供 HH:MM sync_time")
    return strategy, normalized_time


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


def offline_prepare_plan(
    *,
    now: datetime,
    timezone_name: str,
    schedule_times: Sequence[str],
    prefetch_lead_minutes: int,
    server_margin_minutes: int,
    future_prepare_hour_local: int,
    sync_strategy: str = "first_display_lead",
    sync_time: str | None = None,
    minimum_gap_minutes: int = MINIMUM_SCHEDULE_GAP_MINUTES,
    maximum_slots: int = MAX_OFFLINE_SLOTS,
    skip_target_dates: Sequence[str] = (),
) -> OfflinePreparePlan:
    """Resolve due local dates and the next persisted deadline once.

    This is the canonical server preparation policy.  The Scheduler consumes
    ``due_target_dates`` while the repository persists ``next_deadline``;
    keeping both projections here prevents their today/tomorrow semantics from
    drifting apart.
    """

    if type(prefetch_lead_minutes) is not int or not 0 <= prefetch_lead_minutes <= 120:
        raise ValueError("DEVICE-008 prefetch_lead_minutes 不合法")
    if type(server_margin_minutes) is not int or not 0 <= server_margin_minutes <= 60:
        raise ValueError("DEVICE-008 server_prefetch_margin_minutes 不合法")
    if type(future_prepare_hour_local) is not int or not 0 <= future_prepare_hour_local <= 23:
        raise ValueError("DEVICE-008 future_schedule_prepare_hour_local 不合法")
    try:
        zone = ZoneInfo(str(timezone_name))
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("DEVICE-008 裝置 IANA 時區不合法") from exc
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    local_now = current.astimezone(zone)
    strategy, normalized_sync_time = normalize_sync_strategy(sync_strategy, sync_time)
    slots = validate_offline_schedule(
        list(schedule_times),
        maximum=resolve_offline_schedule_max_slots(
            {"offline_schedule_max_slots": maximum_slots}
        ),
        minimum_gap_minutes=minimum_gap_minutes,
    )
    skipped = {str(value) for value in skip_target_dates}
    lead_and_margin = timedelta(minutes=prefetch_lead_minutes + server_margin_minutes)

    def slot_at(target: date, slot: str) -> datetime:
        hour, minute = (int(part) for part in slot.split(":"))
        return datetime.combine(target, time(hour, minute), tzinfo=zone)

    def technical_deadline(target: date) -> datetime:
        if strategy == "fixed_daily":
            assert normalized_sync_time is not None
            hour, minute = (int(part) for part in normalized_sync_time.split(":"))
            return datetime.combine(target, time(hour, minute), tzinfo=zone)
        return slot_at(target, slots[0]) - lead_and_margin

    today = local_now.date()
    due: list[date] = []
    if today.isoformat() not in skipped:
        today_technical = technical_deadline(today)
        if local_now >= today_technical and any(
            slot_at(today, slot) > local_now for slot in slots
        ):
            due.append(today)
    tomorrow = today + timedelta(days=1)
    if tomorrow.isoformat() not in skipped:
        handoff = datetime.combine(
            today,
            time(future_prepare_hour_local, 0),
            tzinfo=zone,
        )
        if local_now >= min(technical_deadline(tomorrow), handoff):
            due.append(tomorrow)

    next_deadline: datetime | None = None
    for offset in range(3):
        target = today + timedelta(days=offset)
        if target.isoformat() in skipped:
            continue
        technical = technical_deadline(target)
        if offset == 0:
            if local_now < technical:
                next_deadline = technical
                break
            if any(slot_at(target, slot) > local_now for slot in slots):
                next_deadline = local_now
                break
            continue
        handoff = datetime.combine(
            target - timedelta(days=1),
            time(future_prepare_hour_local, 0),
            tzinfo=zone,
        )
        candidate = min(technical, handoff)
        next_deadline = local_now if local_now >= candidate else candidate
        break
    if next_deadline is None:
        next_deadline = local_now + timedelta(minutes=15)
    return OfflinePreparePlan(
        due_target_dates=tuple(due),
        next_deadline=next_deadline.astimezone(timezone.utc),
    )


def schedule_minutes(value: str) -> int:
    if _TIME.fullmatch(str(value)) is None:
        raise ValueError("DEVICE-008 時刻格式錯誤")
    hour, minute = (int(part) for part in str(value).split(":"))
    return hour * 60 + minute


def prefetch_slots(
    values: list[str], *, lead_minutes: int = 5, minimum_gap_minutes: int = MINIMUM_SCHEDULE_GAP_MINUTES
) -> list[tuple[str, str]]:
    schedule = validate_offline_schedule(values, maximum=MAX_OFFLINE_SLOTS, minimum_gap_minutes=minimum_gap_minutes)
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
    minimum_gap_minutes: int = MINIMUM_SCHEDULE_GAP_MINUTES,
    maximum_slots: int = MAX_OFFLINE_SLOTS,
) -> list[str]:
    """Return UTC deadlines after the following slot, not after this slot.

    A prefetched image may be downloaded well before its display time.  Its
    queue reservation therefore remains valid until the next scheduled slot
    (plus a small grace period); expiring it immediately after its own display
    time would discard a still-valid offline queue item.
    """

    if type(grace_minutes) is not int or not 0 <= grace_minutes <= 1440:
        raise ValueError("DEVICE-008 slot deadline 寬限分鐘必須介於 0 到 1440")
    schedule = validate_offline_schedule(
        values,
        maximum=resolve_offline_schedule_max_slots({"offline_schedule_max_slots": maximum_slots}),
        minimum_gap_minutes=minimum_gap_minutes,
    )
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


def next_sync_epoch(
    *,
    now: datetime,
    schedule: list[str],
    timezone_name: str,
    lead_minutes: int = 0,
    sync_strategy: str = "first_display_lead",
    sync_time: str | None = None,
    minimum_gap_minutes: int = MINIMUM_SCHEDULE_GAP_MINUTES,
    maximum_slots: int = MAX_OFFLINE_SLOTS,
) -> int:
    """Return the next configured network-sync epoch in the device timezone."""
    if type(lead_minutes) is not int or not 0 <= lead_minutes <= 120:
        raise ValueError("DEVICE-008 prefetch 提前分鐘必須介於 0 到 120")
    strategy, normalized_sync_time = normalize_sync_strategy(sync_strategy, sync_time)
    zone = ZoneInfo(timezone_name)
    local = now.astimezone(zone)
    slots = validate_offline_schedule(
        schedule,
        maximum=resolve_offline_schedule_max_slots({"offline_schedule_max_slots": maximum_slots}),
        minimum_gap_minutes=minimum_gap_minutes,
    )
    candidates: list[datetime] = []
    for day_offset in (0, 1):
        target_date = local.date() + timedelta(days=day_offset)
        if strategy == "fixed_daily":
            assert normalized_sync_time is not None
            hour, minute = (int(part) for part in normalized_sync_time.split(":"))
            candidate = datetime.combine(target_date, time(hour, minute), tzinfo=zone)
            if candidate > local:
                candidates.append(candidate)
            continue
        for slot in slots:
            hour, minute = (int(part) for part in slot.split(":"))
            candidate = datetime.combine(target_date, time(hour, minute), tzinfo=zone)
            sync_at = candidate - timedelta(minutes=int(lead_minutes))
            if sync_at > local:
                candidates.append(sync_at)
            elif candidate > local and not candidates:
                # A display boundary is already inside the lead window.  The
                # correct action is an immediate wake, never a stale 24-hour
                # relative timer.
                candidates.append(local)
    if not candidates:
        raise ValueError("DEVICE-008 找不到下一個網路同步時刻")
    return int(min(candidates).timestamp())


def next_sleep_epoch(
    *,
    now: datetime,
    schedule: list[str],
    timezone_name: str,
    lead_minutes: int = 0,
    sync_strategy: str = "first_display_lead",
    sync_time: str | None = None,
    minimum_gap_minutes: int = MINIMUM_SCHEDULE_GAP_MINUTES,
    maximum_slots: int = MAX_OFFLINE_SLOTS,
) -> int:
    """Backward-compatible alias for the configured next network wake."""
    return next_sync_epoch(
        now=now,
        schedule=schedule,
        timezone_name=timezone_name,
        lead_minutes=lead_minutes,
        sync_strategy=sync_strategy,
        sync_time=sync_time,
        minimum_gap_minutes=minimum_gap_minutes,
        maximum_slots=maximum_slots,
    )
