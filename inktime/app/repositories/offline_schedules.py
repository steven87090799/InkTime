"""Atomic server-side preparation and device projection for offline schedules."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Sequence
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from inktime.app.core.paths import UnsafePathError
from inktime.app.db import Database
from inktime.app.domain.photopainter.offline_schedule import (
    MINIMUM_SCHEDULE_GAP_MINUTES,
    normalize_sync_strategy,
    slot_deadlines,
    validate_offline_schedule,
)
from inktime.app.services.device_releases import payload_entry_from_manifest


_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_PREPARED_SLOTS = 24
TERMINAL_PREPARATION_OUTCOMES = frozenset({"NO_CONTENT", "NO_ELIGIBLE_CANDIDATES"})
RECOVERABLE_SHORTAGE_CODES = frozenset({"NO_CONTENT", "NO_ELIGIBLE_CANDIDATES"})
SHORTAGE_RETRY_COOLDOWN_SECONDS = 3600


@dataclass(frozen=True)
class RetryAfterDetails:
    retry_after_epoch: int
    next_slot_epoch: int | None


class OfflineScheduleRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def playlist_version(
        *,
        device_id: str,
        target_date: str,
        config_version: int,
        schedule_times: Sequence[str],
        release_ids: Sequence[str],
    ) -> str:
        """Return the immutable identity of one prepared device-day playlist."""

        material = {
            "device_id": str(device_id),
            "target_date": str(target_date),
            "config_version": int(config_version),
            "schedule_times": [str(value) for value in schedule_times],
            "release_ids": [str(value) for value in release_ids],
        }
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _date(value: str) -> date:
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError("DEVICE-008 target_date 必須是 YYYY-MM-DD") from exc

    @staticmethod
    def _show_at(target_date: date, slot: str, timezone_name: str) -> str:
        hour, minute = (int(part) for part in slot.split(":"))
        local = datetime.combine(target_date, time(hour, minute), tzinfo=ZoneInfo(timezone_name))
        return local.astimezone(timezone.utc).isoformat()

    @staticmethod
    def retry_after_epoch(
        *,
        now: datetime,
        timezone_name: str,
        schedule_times: Sequence[str],
        prefetch_lead_minutes: int,
        server_margin_minutes: int,
        sync_strategy: str = "first_display_lead",
        sync_time: str | None = None,
        minimum_gap_minutes: int = MINIMUM_SCHEDULE_GAP_MINUTES,
    ) -> int:
        """Return the retry epoch while retaining the legacy scalar API."""

        return OfflineScheduleRepository.retry_after_details(
            now=now,
            timezone_name=timezone_name,
            schedule_times=schedule_times,
            prefetch_lead_minutes=prefetch_lead_minutes,
            server_margin_minutes=server_margin_minutes,
            sync_strategy=sync_strategy,
            sync_time=sync_time,
            minimum_gap_minutes=minimum_gap_minutes,
        ).retry_after_epoch

    @staticmethod
    def retry_after_details(
        *,
        now: datetime,
        timezone_name: str,
        schedule_times: Sequence[str],
        prefetch_lead_minutes: int,
        server_margin_minutes: int,
        sync_strategy: str = "first_display_lead",
        sync_time: str | None = None,
        minimum_gap_minutes: int = MINIMUM_SCHEDULE_GAP_MINUTES,
    ) -> RetryAfterDetails:
        """Choose a bounded retry without skipping a remaining local slot."""

        if not 0 <= int(prefetch_lead_minutes) <= 120:
            raise ValueError("DEVICE-008 prefetch_lead_minutes 不合法")
        if not 0 <= int(server_margin_minutes) <= 60:
            raise ValueError("DEVICE-008 server_prefetch_margin_minutes 不合法")
        try:
            zone = ZoneInfo(str(timezone_name))
        except (TypeError, ValueError) as exc:
            raise ValueError("DEVICE-008 裝置 IANA 時區不合法") from exc
        local_now = now
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=timezone.utc)
        local_now = local_now.astimezone(zone)
        strategy, normalized_sync_time = normalize_sync_strategy(sync_strategy, sync_time)
        slots = validate_offline_schedule(
            schedule_times,
            maximum=MAX_PREPARED_SLOTS,
            minimum_gap_minutes=minimum_gap_minutes,
        )
        lead = int(prefetch_lead_minutes)
        margin = int(server_margin_minutes)

        def slot_at(target: date, slot: str) -> datetime:
            hour, minute = (int(part) for part in slot.split(":"))
            return datetime.combine(target, time(hour, minute), tzinfo=zone)

        def epoch(value: datetime) -> int:
            return int(value.astimezone(timezone.utc).timestamp())

        def retry_before_slots(slot_epochs: list[int]) -> RetryAfterDetails | None:
            for slot_epoch in slot_epochs:
                # Integer epochs deliberately leave a one-second guard.  A
                # sub-second "almost due" Slot is not a valid retry target;
                # continue to the next serviceable Slot instead.
                if slot_epoch <= now_epoch + 1:
                    continue
                safe_deadline = slot_epoch - min(lead, 5) * 60
                candidate = min(now_epoch + 15 * 60, safe_deadline)
                candidate = max(candidate, now_epoch + 60)
                if candidate >= slot_epoch:
                    candidate = slot_epoch - 1
                if now_epoch < candidate < slot_epoch:
                    return RetryAfterDetails(candidate, slot_epoch)
            return None

        def retry_for_day(target: date, *, allow_prepare_point: bool) -> RetryAfterDetails | None:
            slot_epochs = [epoch(slot_at(target, slot)) for slot in slots]
            if allow_prepare_point:
                if strategy == "fixed_daily":
                    assert normalized_sync_time is not None
                    sync_hour, sync_minute = (int(part) for part in normalized_sync_time.split(":"))
                    prepare_epoch = epoch(
                        datetime.combine(target, time(sync_hour, sync_minute), tzinfo=zone)
                    )
                else:
                    prepare_epoch = slot_epochs[0] - (lead + margin) * 60
                if now_epoch < prepare_epoch < slot_epochs[0]:
                    return RetryAfterDetails(prepare_epoch, slot_epochs[0])
            return retry_before_slots(slot_epochs)

        today = local_now.date()
        now_epoch = epoch(local_now)

        # Rule A/B: stay on today while its first prepare point is due and a
        # serviceable future Slot remains.  The helper uses only integer
        # epochs, so an imminent first Slot cannot fall into a generic retry.
        today_details = retry_for_day(today, allow_prepare_point=True)
        if today_details is not None:
            return today_details

        # Rule C: only after today's serviceable Slots are exhausted may the
        # retry move to tomorrow's first prepare point.  Preserve the legacy
        # null next_slot_epoch for this normal cross-day transition.
        tomorrow = today + timedelta(days=1)
        tomorrow_slots = [epoch(slot_at(tomorrow, slot)) for slot in slots]
        if strategy == "fixed_daily":
            assert normalized_sync_time is not None
            sync_hour, sync_minute = (int(part) for part in normalized_sync_time.split(":"))
            tomorrow_prepare = epoch(
                datetime.combine(tomorrow, time(sync_hour, sync_minute), tzinfo=zone)
            )
        else:
            tomorrow_prepare = tomorrow_slots[0] - (lead + margin) * 60
        if now_epoch < tomorrow_prepare < tomorrow_slots[0]:
            return RetryAfterDetails(tomorrow_prepare, None)
        tomorrow_details = retry_before_slots(tomorrow_slots)
        if tomorrow_details is not None:
            return tomorrow_details
        # There is no representable integer strictly before an imminent
        # boundary Slot.  Keep recovery bounded at the next integer rather
        # than turning a normal time boundary into a generic API fallback.
        return RetryAfterDetails(now_epoch + 1, None)

    @staticmethod
    def retry_after_target_details(
        *,
        now: datetime,
        timezone_name: str,
        target_date: str,
        schedule_times: Sequence[str],
        prefetch_lead_minutes: int,
        server_margin_minutes: int,
        sync_strategy: str = "first_display_lead",
        sync_time: str | None = None,
        minimum_gap_minutes: int = MINIMUM_SCHEDULE_GAP_MINUTES,
    ) -> RetryAfterDetails:
        """Return a bounded retry for the explicitly bounded next target day."""

        target = OfflineScheduleRepository._date(target_date)
        if not 0 <= int(prefetch_lead_minutes) <= 120:
            raise ValueError("DEVICE-008 prefetch_lead_minutes 不合法")
        if not 0 <= int(server_margin_minutes) <= 60:
            raise ValueError("DEVICE-008 server_prefetch_margin_minutes 不合法")
        try:
            zone = ZoneInfo(str(timezone_name))
        except (TypeError, ValueError) as exc:
            raise ValueError("DEVICE-008 裝置 IANA 時區不合法") from exc
        local_now = now
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=timezone.utc)
        local_now = local_now.astimezone(zone)
        strategy, normalized_sync_time = normalize_sync_strategy(sync_strategy, sync_time)
        slots = validate_offline_schedule(
            schedule_times,
            maximum=MAX_PREPARED_SLOTS,
            minimum_gap_minutes=minimum_gap_minutes,
        )
        lead = int(prefetch_lead_minutes)
        margin = int(server_margin_minutes)
        now_epoch = int(local_now.astimezone(timezone.utc).timestamp())

        def slot_epoch(day: date, slot: str) -> int:
            hour, minute = (int(part) for part in slot.split(":"))
            return int(
                datetime.combine(day, time(hour, minute), tzinfo=zone)
                .astimezone(timezone.utc)
                .timestamp()
            )

        epochs = [slot_epoch(target, slot) for slot in slots]
        if strategy == "fixed_daily":
            assert normalized_sync_time is not None
            sync_hour, sync_minute = (int(part) for part in normalized_sync_time.split(":"))
            prepare_epoch = int(
                datetime.combine(target, time(sync_hour, sync_minute), tzinfo=zone)
                .astimezone(timezone.utc)
                .timestamp()
            )
        else:
            prepare_epoch = epochs[0] - (lead + margin) * 60
        if now_epoch < prepare_epoch < epochs[0]:
            return RetryAfterDetails(prepare_epoch, epochs[0])
        for candidate_slot in epochs:
            if candidate_slot <= now_epoch + 1:
                continue
            safe_deadline = candidate_slot - min(lead, 5) * 60
            retry_epoch = min(now_epoch + 15 * 60, safe_deadline)
            retry_epoch = max(retry_epoch, now_epoch + 60)
            if retry_epoch >= candidate_slot:
                retry_epoch = candidate_slot - 1
            if now_epoch < retry_epoch < candidate_slot:
                return RetryAfterDetails(retry_epoch, candidate_slot)
        # At the integer boundary there may be no representable epoch strictly
        # before the first Slot.  Return the next integer wake without a
        # generic 15-minute exception path; callers keep next_slot nullable.
        return RetryAfterDetails(now_epoch + 1, None)

    @staticmethod
    def _manifest_entry(manifest_json: str) -> dict[str, Any]:
        try:
            manifest = json.loads(manifest_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("QUEUE-002 Release Manifest 不可解析") from exc
        try:
            return payload_entry_from_manifest(manifest)
        except (ValueError, UnsafePathError) as exc:
            raise ValueError("QUEUE-002 Release Payload 不符合完整性契約") from exc

    def _row(self, connection, schedule_id: str) -> dict[str, Any] | None:
        schedule = connection.execute(
            "SELECT * FROM device_offline_schedules WHERE id=?", (schedule_id,)
        ).fetchone()
        if schedule is None:
            return None
        slots = connection.execute(
            """
            SELECT s.id AS slot_id,s.slot_index,s.show_at,s.release_id,s.queue_item_id,s.sha256,
                   r.width,r.height,r.pixel_format,r.render_profile,r.manifest_json,
                   q.queue_version,qi.offline_prefetch_allowed,qi.offline_schedule_id,
                   qi.ack_deadline,qi.terminal_ack_retention
            FROM device_offline_schedule_slots s
            JOIN releases r ON r.id=s.release_id
            JOIN device_content_queue_items qi ON qi.id=s.queue_item_id
            LEFT JOIN device_content_queues q ON q.device_id=qi.device_id
            WHERE schedule_id=? ORDER BY slot_index
            """,
            (schedule_id,),
        ).fetchall()
        normalized_slots: list[dict[str, Any]] = []
        for raw_slot in slots:
            slot = dict(raw_slot)
            try:
                manifest = json.loads(str(slot.pop("manifest_json") or "{}"))
                entry = self._manifest_entry(json.dumps(manifest, ensure_ascii=False))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("QUEUE-002 Release Manifest 不符合離線排程契約") from exc
            filename = str(entry["name"])
            try:
                show_at_epoch = int(datetime.fromisoformat(str(slot["show_at"])).timestamp())
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("QUEUE-002 離線排程 show_at 時間格式不合法") from exc
            slot.update(
                {
                    "sha256": str(entry["sha256"]).lower(),
                    "source_photo_id": str(entry.get("source_photo_id") or "") or None,
                    "show_at_epoch": show_at_epoch,
                    "size": int(entry["size"]),
                    "width": int(slot["width"]),
                    "height": int(slot["height"]),
                    "pixel_format": str(slot["pixel_format"]),
                    "render_profile": str(slot["render_profile"]),
                    "queue_version": int(slot["queue_version"] or 0),
                    "offline_prefetch_allowed": bool(slot["offline_prefetch_allowed"]),
                    "download_url": (
                        f"/api/device/v1/queue/items/{quote(str(slot['queue_item_id']), safe='')}/files/"
                        f"{quote(filename, safe='')}"
                    ),
                }
            )
            normalized_slots.append(slot)
        try:
            snapshot_json = json.loads(str(schedule["snapshot_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            snapshot_json = {}
        playlist_version = str(
            snapshot_json.get("playlist_version") if isinstance(snapshot_json, dict) else ""
        )
        if not playlist_version:
            try:
                schedule_times = json.loads(str(schedule["schedule_times_json"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                schedule_times = []
            release_ids = [str(slot["release_id"]) for slot in normalized_slots]
            if isinstance(schedule_times, list) and len(schedule_times) == len(release_ids) and release_ids:
                playlist_version = self.playlist_version(
                    device_id=str(schedule["device_id"]),
                    target_date=str(schedule["target_date"]),
                    config_version=int(schedule["config_version"]),
                    schedule_times=schedule_times,
                    release_ids=release_ids,
                )
        device_snapshot = {
            "id": str(schedule["device_id"]),
            "panel_profile": str(schedule["panel_profile"]),
            "rotation": int(schedule["rotation"]),
            "timezone": str(schedule["timezone"]),
            "schedule_times_json": str(schedule["schedule_times_json"]),
            "prefetch_lead_minutes": int(schedule["prefetch_lead_minutes"]),
            "button_wake_action": str(schedule["button_wake_action"]),
            "offline_schedule_version": int(schedule["offline_schedule_version"]),
            "minimum_schedule_gap_minutes": int(
                schedule["minimum_schedule_gap_minutes"] or MINIMUM_SCHEDULE_GAP_MINUTES
            ),
            "sync_strategy": str(schedule["sync_strategy"] or "first_display_lead"),
            "sync_time": schedule["sync_time"],
            "snapshot_json": snapshot_json if isinstance(snapshot_json, dict) else {},
        }
        return {
            "schedule": dict(schedule),
            "device": device_snapshot,
            "playlist_version": playlist_version,
            "slots": normalized_slots,
        }

    def latest_for_device(self, device_id: str) -> dict[str, Any] | None:
        with self.database.session() as connection:
            row = connection.execute(
                """
                SELECT device_offline_schedules.id AS id FROM device_offline_schedules
                WHERE device_id=? AND status='ready'
                ORDER BY target_date DESC,config_version DESC,created_at DESC
                LIMIT 1
                """,
                (device_id,),
            ).fetchone()
            return self._row(connection, str(row["id"])) if row else None

    def terminal_outcome_for_device(
        self,
        *,
        device_id: str,
        target_date: str,
        config_version: int,
    ) -> dict[str, Any] | None:
        """Return a terminal no-content result for one exact device-day config."""

        with self.database.session() as connection:
            row = connection.execute(
                """
                SELECT id,status,terminal_outcome_code,updated_at
                FROM device_offline_schedules
                WHERE device_id=?
                  AND target_date=?
                  AND config_version=?
                  AND status='failed'
                  AND terminal_outcome_code IN ('NO_CONTENT','NO_ELIGIBLE_CANDIDATES')
                ORDER BY updated_at DESC,id DESC LIMIT 1
                """,
                (device_id, self._date(target_date).isoformat(), int(config_version)),
            ).fetchone()
        return dict(row) if row else None

    def claim_terminal_outcome_retry(
        self,
        *,
        terminal_outcome: dict[str, Any] | None,
        device_id: str,
        target_date: str,
        config_version: int,
        now: datetime,
        retry_after_seconds: int = SHORTAGE_RETRY_COOLDOWN_SECONDS,
    ) -> bool:
        """Atomically claim one recovery window after a shortage cooldown."""

        if terminal_outcome is None:
            return False
        outcome_code = str(terminal_outcome.get("terminal_outcome_code"))
        if outcome_code not in RECOVERABLE_SHORTAGE_CODES:
            return False
        try:
            updated_at = datetime.fromisoformat(str(terminal_outcome["updated_at"]))
            schedule_id = str(terminal_outcome["id"])
        except (KeyError, TypeError, ValueError):
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        cooldown = max(0, int(retry_after_seconds))
        cooldown_expires_at = updated_at.astimezone(timezone.utc) + timedelta(seconds=cooldown)
        if current.astimezone(timezone.utc) < cooldown_expires_at:
            return False
        claimed_at = current.astimezone(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE device_offline_schedules
                SET updated_at=?
                WHERE id=? AND device_id=? AND target_date=? AND config_version=?
                  AND status='failed' AND terminal_outcome_code=? AND updated_at=?
                """,
                (
                    claimed_at,
                    schedule_id,
                    device_id,
                    self._date(target_date).isoformat(),
                    int(config_version),
                    outcome_code,
                    str(terminal_outcome["updated_at"]),
                ),
            )
        return bool(cursor.rowcount)

    def record_terminal_outcome(
        self,
        *,
        device_id: str,
        target_date: str,
        config_version: int,
        outcome_code: str,
        message: str,
    ) -> dict[str, Any]:
        """Persist deterministic shortage without creating a partial playlist."""

        normalized_code = str(outcome_code).strip()
        if normalized_code not in TERMINAL_PREPARATION_OUTCOMES:
            raise ValueError("DEVICE-008 terminal preparation outcome 不合法")
        day = self._date(target_date).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            device = connection.execute(
                """
                SELECT id,config_version,timezone,panel_profile,rotation,schedule_times_json,
                       prefetch_lead_minutes,button_wake_action,offline_schedule_version,
                       minimum_schedule_gap_minutes,sync_strategy,sync_time
                FROM devices WHERE id=? AND enabled=1
                """,
                (device_id,),
            ).fetchone()
            if device is None:
                raise KeyError(device_id)
            if int(device["config_version"]) != int(config_version):
                raise ValueError("DISPLAY-CONFIG-RACE 裝置設定已變更，拒絕記錄離線結果")
            existing = connection.execute(
                """
                SELECT id,status FROM device_offline_schedules
                WHERE device_id=? AND target_date=? AND config_version=?
                """,
                (device_id, day, int(config_version)),
            ).fetchone()
            if existing is not None and str(existing["status"]) == "ready":
                return {"status": "ready", "idempotent": True, "id": str(existing["id"])}
            schedule_id = str(existing["id"]) if existing is not None else str(uuid4())
            snapshot = {
                "config_version": int(config_version),
                "outcome_code": normalized_code,
                "message": str(message)[:500],
            }
            connection.execute(
                """
                INSERT INTO device_offline_schedules(
                    id,device_id,target_date,config_version,timezone,status,created_at,updated_at,
                    panel_profile,rotation,schedule_times_json,prefetch_lead_minutes,
                    button_wake_action,offline_schedule_version,minimum_schedule_gap_minutes,
                    sync_strategy,sync_time,snapshot_json,terminal_outcome_code
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(device_id,target_date,config_version) DO UPDATE SET
                    status='failed',updated_at=excluded.updated_at,
                    snapshot_json=excluded.snapshot_json,
                    terminal_outcome_code=excluded.terminal_outcome_code
                """,
                (
                    schedule_id,
                    device_id,
                    day,
                    int(config_version),
                    str(device["timezone"]),
                    "failed",
                    now,
                    now,
                    str(device["panel_profile"]),
                    int(device["rotation"]),
                    str(device["schedule_times_json"] or "[]"),
                    int(device["prefetch_lead_minutes"]),
                    str(device["button_wake_action"]),
                    int(device["offline_schedule_version"]),
                    int(device["minimum_schedule_gap_minutes"] or MINIMUM_SCHEDULE_GAP_MINUTES),
                    str(device["sync_strategy"] or "first_display_lead"),
                    device["sync_time"],
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    normalized_code,
                ),
            )
        return {
            "status": "completed",
            "outcome": "no_content",
            "outcome_code": normalized_code,
            "idempotent": existing is not None,
        }

    def ready_for_device(
        self,
        *,
        device_id: str,
        target_date: str,
        config_version: int,
    ) -> dict[str, Any] | None:
        """Return an exact ready day used by scheduler idempotency checks."""

        with self.database.session() as connection:
            row = connection.execute(
                """
                SELECT device_offline_schedules.id AS id FROM device_offline_schedules
                JOIN devices d ON d.id=device_offline_schedules.device_id
                WHERE device_offline_schedules.device_id=?
                  AND device_offline_schedules.target_date=?
                  AND device_offline_schedules.config_version=?
                  AND device_offline_schedules.status='ready'
                  AND d.delivery_mode='inktime_offline_schedule'
                  AND d.offline_prefetch_allowed=1
                ORDER BY device_offline_schedules.updated_at DESC,
                         device_offline_schedules.id DESC LIMIT 1
                """,
                (device_id, self._date(target_date).isoformat(), int(config_version)),
            ).fetchone()
            return self._row(connection, str(row["id"])) if row else None

    @staticmethod
    def _is_future_item(value: object, now: str) -> bool:
        if not value:
            return False
        try:
            item_time = datetime.fromisoformat(str(value))
            current_time = datetime.fromisoformat(str(now))
        except (TypeError, ValueError):
            return False
        if item_time.tzinfo is None:
            item_time = item_time.replace(tzinfo=timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        return item_time.astimezone(timezone.utc) > current_time.astimezone(timezone.utc)

    def _supersede_previous_schedule_items(
        self, connection, *, device_id: str, target_date: str, schedule_id: str, now: str
    ) -> int:
        old_schedules = connection.execute(
            """
            SELECT id FROM device_offline_schedules
            WHERE device_id=? AND target_date=? AND id<>? AND status IN ('preparing','ready')
            """,
            (device_id, target_date, schedule_id),
        ).fetchall()
        if not old_schedules:
            return 0
        old_ids = [str(row["id"]) for row in old_schedules]
        placeholders = ",".join("?" for _ in old_ids)
        connection.execute(
            f"UPDATE device_offline_schedules SET status='cancelled',updated_at=? "  # noqa: S608
            f"WHERE id IN ({placeholders})",
            (now, *old_ids),
        )
        items = connection.execute(
            f"SELECT id,status,display_after,delivery_mode,offline_slot,offline_schedule_id "  # noqa: S608
            f"FROM device_content_queue_items WHERE device_id=? AND offline_schedule_id IN ({placeholders}) "
            "AND status NOT IN ('DISPLAYED','FAILED','EXPIRED','CANCELLED') ORDER BY id",
            (device_id, *old_ids),
        ).fetchall()
        cancelled = 0
        for item in items:
            if not self._is_future_item(item["display_after"], now):
                continue
            item_id = str(item["id"])
            connection.execute(
                "UPDATE device_content_queue_items SET status='CANCELLED',last_error_code='QUEUE-005',updated_at=? WHERE id=?",
                (now, item_id),
            )
            payload = {
                "reason": "device_schedule_changed",
                "change_kind": "offline_schedule_reprepared",
                "offline_slot": item["offline_slot"],
                "offline_schedule_id": item["offline_schedule_id"],
                "target_date": target_date,
            }
            connection.execute(
                "INSERT OR IGNORE INTO device_content_queue_events(queue_item_id,device_id,event_type,idempotency_key,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                (
                    item_id,
                    device_id,
                    "FUTURE_SLOT_SUPERSEDED",
                    f"FUTURE_SLOT_SUPERSEDED:{device_id}:{item_id}",
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
            connection.execute(
                "UPDATE rollout_targets SET status='cancelled_future_schedule',last_error_code='QUEUE-005',updated_at=? WHERE queue_item_id=?",
                (now, item_id),
            )
            cancelled += 1
        return cancelled

    def prepare_day(
        self,
        *,
        device_id: str,
        target_date: str,
        release_ids: Sequence[str],
        expected_config_version: int | None = None,
    ) -> dict[str, Any]:
        day = self._date(target_date)
        if not 1 <= len(release_ids) <= MAX_PREPARED_SLOTS:
            raise ValueError("DEVICE-008 一日離線排程最多 24 個 Slot")
        normalized_release_ids = [str(value).strip() for value in release_ids]
        if any(_RELEASE_ID.fullmatch(value) is None for value in normalized_release_ids):
            raise ValueError("QUEUE-002 Release ID 不合法")
        if len(set(normalized_release_ids)) != len(normalized_release_ids):
            raise ValueError("DEVICE-008 一個 Slot 不可重複使用同一 Release")
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                device = connection.execute(
                    """
                    SELECT timezone,config_version,delivery_mode,offline_prefetch_allowed,
                           panel_profile,rotation,schedule_times_json,prefetch_lead_minutes,
                           button_wake_action,offline_schedule_version,
                           minimum_schedule_gap_minutes,sync_strategy,sync_time
                    FROM devices WHERE id=? AND enabled=1
                    """,
                    (device_id,),
                ).fetchone()
                if device is None:
                    raise KeyError(device_id)
                current_config_version = int(device["config_version"])
                if expected_config_version is not None and current_config_version != int(
                    expected_config_version
                ):
                    raise ValueError("DISPLAY-CONFIG-RACE 裝置設定已變更，拒絕提交舊離線排程")
                if str(device["delivery_mode"]) != "inktime_offline_schedule" or not bool(
                    device["offline_prefetch_allowed"]
                ):
                    raise ValueError("QUEUE-005 裝置未啟用離線排程或 Prefetch")
                try:
                    configured_times = json.loads(str(device["schedule_times_json"] or "[]"))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError("DEVICE-008 裝置 schedule_times 不可解析") from exc
                minimum_gap_minutes = int(
                    device["minimum_schedule_gap_minutes"] or MINIMUM_SCHEDULE_GAP_MINUTES
                )
                sync_strategy, sync_time = normalize_sync_strategy(
                    str(device["sync_strategy"] or "first_display_lead"), device["sync_time"]
                )
                configured_times = validate_offline_schedule(
                    configured_times,
                    maximum=MAX_PREPARED_SLOTS,
                    minimum_gap_minutes=minimum_gap_minutes,
                )
                if len(normalized_release_ids) != len(configured_times):
                    raise ValueError("DEVICE-008 Release 數量必須等於裝置 schedule_times 數量")
                playlist_version = self.playlist_version(
                    device_id=device_id,
                    target_date=day.isoformat(),
                    config_version=current_config_version,
                    schedule_times=configured_times,
                    release_ids=normalized_release_ids,
                )
                existing = connection.execute(
                    """
                    SELECT id,status FROM device_offline_schedules
                    WHERE device_id=? AND target_date=? AND config_version=?
                    """,
                    (device_id, day.isoformat(), current_config_version),
                ).fetchone()
                if existing is not None and str(existing["status"]) == "ready":
                    existing_row = self._row(connection, str(existing["id"])) or {}
                    if str(existing_row.get("playlist_version") or "") == playlist_version:
                        return existing_row
                    raise ValueError(
                        "QUEUE-005 相同裝置、日期與設定版本已有不同 Playlist；請只替換指定 Slot"
                    )
                schedule_id = str(existing["id"]) if existing is not None else str(uuid4())
                snapshot = {
                    "panel_profile": str(device["panel_profile"]),
                    "rotation": int(device["rotation"]),
                    "timezone": str(device["timezone"]),
                    "schedule_times": list(configured_times),
                    "prefetch_lead_minutes": int(device["prefetch_lead_minutes"]),
                    "button_wake_action": str(device["button_wake_action"]),
                    "offline_schedule_version": int(device["offline_schedule_version"]),
                    "minimum_schedule_gap_minutes": minimum_gap_minutes,
                    "sync_strategy": sync_strategy,
                    "sync_time": sync_time,
                    "config_version": current_config_version,
                    "playlist_version": playlist_version,
                }
                connection.execute(
                    """
                    INSERT INTO device_offline_schedules(
                        id,device_id,target_date,config_version,timezone,status,created_at,updated_at,
                        panel_profile,rotation,schedule_times_json,prefetch_lead_minutes,
                        button_wake_action,offline_schedule_version,minimum_schedule_gap_minutes,
                        sync_strategy,sync_time,snapshot_json,terminal_outcome_code
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(device_id,target_date,config_version) DO UPDATE SET
                        status='preparing',updated_at=excluded.updated_at,
                        panel_profile=excluded.panel_profile,rotation=excluded.rotation,
                        schedule_times_json=excluded.schedule_times_json,
                        prefetch_lead_minutes=excluded.prefetch_lead_minutes,
                        button_wake_action=excluded.button_wake_action,
                        offline_schedule_version=excluded.offline_schedule_version,
                        minimum_schedule_gap_minutes=excluded.minimum_schedule_gap_minutes,
                        sync_strategy=excluded.sync_strategy,
                        sync_time=excluded.sync_time,
                        snapshot_json=excluded.snapshot_json,
                        terminal_outcome_code=NULL
                    """,
                    (
                        schedule_id,
                        device_id,
                        day.isoformat(),
                        current_config_version,
                        str(device["timezone"]),
                        "preparing",
                        now,
                        now,
                        str(device["panel_profile"]),
                        int(device["rotation"]),
                        json.dumps(configured_times, ensure_ascii=False, separators=(",", ":")),
                        int(device["prefetch_lead_minutes"]),
                        str(device["button_wake_action"]),
                        int(device["offline_schedule_version"]),
                        minimum_gap_minutes,
                        sync_strategy,
                        sync_time,
                        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        None,
                    ),
                )
                connection.execute(
                    "DELETE FROM device_offline_schedule_slots WHERE schedule_id=?", (schedule_id,)
                )
                # Current and future dates are independent queue owners.  A
                # retry may supersede only genuinely future items; displayed,
                # failed, and past items remain immutable history.
                cancelled = self._supersede_previous_schedule_items(
                    connection,
                    device_id=device_id,
                    target_date=day.isoformat(),
                    schedule_id=schedule_id,
                    now=now,
                )
                queue = connection.execute(
                    "SELECT depth,queue_version FROM device_content_queues WHERE device_id=?",
                    (device_id,),
                ).fetchone()
                if queue is None:
                    connection.execute(
                        "INSERT INTO device_content_queues(device_id,depth,updated_at) VALUES (?,?,?)",
                        (device_id, max(3, len(normalized_release_ids)), now),
                    )
                    queue = connection.execute(
                        "SELECT depth,queue_version FROM device_content_queues WHERE device_id=?",
                        (device_id,),
                    ).fetchone()
                if int(queue["depth"]) < len(normalized_release_ids):
                    raise ValueError("QUEUE-005 Queue depth 小於離線 Slot 數量")
                queue_version = int(queue["queue_version"]) + (1 if cancelled else 0)
                next_position = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(position),0) FROM device_content_queue_items WHERE device_id=?",
                        (device_id,),
                    ).fetchone()[0]
                )
                deadlines = slot_deadlines(
                    day,
                    configured_times,
                    str(device["timezone"]),
                    grace_minutes=15,
                    minimum_gap_minutes=minimum_gap_minutes,
                )
                for slot_index, slot in enumerate(configured_times):
                    release_id = normalized_release_ids[slot_index]
                    release = connection.execute(
                        """
                        SELECT id,manifest_json FROM releases r
                        WHERE r.id=? AND r.status='published'
                          AND r.render_profile=?
                        """,
                        (release_id, str(device["panel_profile"])),
                    ).fetchone()
                    if release is None:
                        raise ValueError("QUEUE-002 Release 不存在、未發布或 Profile 不相容")
                    entry = self._manifest_entry(str(release["manifest_json"]))
                    duplicate = connection.execute(
                        """
                        SELECT 1 FROM device_content_queue_items
                        WHERE device_id=? AND release_id=?
                          AND status IN ('PENDING','READY','AVAILABLE','DOWNLOADED','ACKNOWLEDGED')
                        """,
                        (device_id, release_id),
                    ).fetchone()
                    if duplicate:
                        raise ValueError("QUEUE-005 Release 已在活動 Queue 中，不可重複佔用 Slot")
                    show_at = self._show_at(day, slot, str(device["timezone"]))
                    deadline = deadlines[slot_index]
                    queue_item_id = str(uuid4())
                    position = next_position + slot_index + 1
                    connection.execute(
                        """
                        INSERT INTO device_content_queue_items(
                            id,device_id,release_id,position,priority,display_after,expires_at,status,
                            idempotency_key,delivery_mode,offline_prefetch_allowed,offline_slot,ack_deadline,
                            terminal_ack_retention,offline_schedule_id,created_at,updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            queue_item_id,
                            device_id,
                            release_id,
                            position,
                            100,
                            show_at,
                            deadline,
                            "READY",
                            f"offline:{schedule_id}:{slot_index}",
                            "offline_schedule",
                            1,
                            slot,
                            deadline,
                            (datetime.fromisoformat(deadline) + timedelta(days=7)).isoformat(),
                            schedule_id,
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO device_offline_schedule_slots(
                            id,schedule_id,slot_index,show_at,release_id,queue_item_id,sha256,created_at
                        ) VALUES (?,?,?,?,?,?,?,?)
                        """,
                        (
                            str(uuid4()),
                            schedule_id,
                            slot_index,
                            show_at,
                            release_id,
                            queue_item_id,
                            str(entry["sha256"]).lower(),
                            now,
                        ),
                    )
                    queue_version += 1
                connection.execute(
                    """
                    UPDATE device_content_queues
                    SET queue_version=?,next_queued_release_id=?,updated_at=?
                    WHERE device_id=?
                    """,
                    (queue_version, normalized_release_ids[0], now, device_id),
                )
                connection.execute(
                    "UPDATE device_offline_schedules SET status='ready',terminal_outcome_code=NULL,updated_at=? WHERE id=?",
                    (now, schedule_id),
                )
                current = connection.execute(
                    """
                    SELECT config_version,panel_profile,rotation,timezone,schedule_times_json,
                           prefetch_lead_minutes,button_wake_action,offline_schedule_version,
                           minimum_schedule_gap_minutes,sync_strategy,sync_time
                    FROM devices WHERE id=? AND enabled=1
                    """,
                    (device_id,),
                ).fetchone()
                if current is None or (
                    int(current["config_version"]) != current_config_version
                    or str(current["panel_profile"]) != str(device["panel_profile"])
                    or int(current["rotation"]) != int(device["rotation"])
                    or str(current["timezone"]) != str(device["timezone"])
                    or str(current["schedule_times_json"]) != str(device["schedule_times_json"])
                    or int(current["prefetch_lead_minutes"]) != int(device["prefetch_lead_minutes"])
                    or str(current["button_wake_action"]) != str(device["button_wake_action"])
                    or int(current["offline_schedule_version"])
                    != int(device["offline_schedule_version"])
                    or int(current["minimum_schedule_gap_minutes"] or MINIMUM_SCHEDULE_GAP_MINUTES)
                    != minimum_gap_minutes
                    or str(current["sync_strategy"] or "first_display_lead") != sync_strategy
                    or (current["sync_time"] or None) != (sync_time or None)
                ):
                    raise ValueError("DISPLAY-CONFIG-RACE 裝置設定在離線排程提交前已變更")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            result = self._row(connection, schedule_id)
            if result is None:
                raise RuntimeError("DEVICE-008 離線排程準備結果不存在")
            result["playlist_version"] = playlist_version
            return result

    def replace_slot(
        self,
        *,
        device_id: str,
        schedule_id: str,
        slot_index: int,
        release_id: str,
        expected_config_version: int | None = None,
    ) -> dict[str, Any]:
        """Replace one future playlist slot without reselecting its siblings."""

        if type(slot_index) is not int or slot_index < 0:
            raise ValueError("DEVICE-008 slot_index 不合法")
        normalized_release_id = str(release_id).strip()
        if _RELEASE_ID.fullmatch(normalized_release_id) is None:
            raise ValueError("QUEUE-002 Release ID 不合法")
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                schedule = connection.execute(
                    """
                    SELECT * FROM device_offline_schedules
                    WHERE id=? AND device_id=? AND status='ready'
                    """,
                    (schedule_id, device_id),
                ).fetchone()
                if schedule is None:
                    raise KeyError(schedule_id)
                device = connection.execute(
                    "SELECT enabled,delivery_mode,offline_prefetch_allowed,config_version FROM devices WHERE id=?",
                    (device_id,),
                ).fetchone()
                if device is None or not bool(device["enabled"]):
                    raise KeyError(device_id)
                if (
                    str(device["delivery_mode"]) != "inktime_offline_schedule"
                    or not bool(device["offline_prefetch_allowed"])
                    or int(device["config_version"]) != int(schedule["config_version"])
                ):
                    raise ValueError("DISPLAY-CONFIG-RACE 裝置設定已變更，拒絕替換舊排程 Slot")
                if expected_config_version is not None and int(schedule["config_version"]) != int(
                    expected_config_version
                ):
                    raise ValueError("DISPLAY-CONFIG-RACE 裝置設定已變更，拒絕替換舊排程 Slot")
                slot = connection.execute(
                    """
                    SELECT * FROM device_offline_schedule_slots
                    WHERE schedule_id=? AND slot_index=?
                    """,
                    (schedule_id, slot_index),
                ).fetchone()
                if slot is None:
                    raise IndexError(slot_index)
                queue_item = connection.execute(
                    "SELECT * FROM device_content_queue_items WHERE id=? AND device_id=?",
                    (slot["queue_item_id"], device_id),
                ).fetchone()
                if queue_item is None or str(queue_item["status"]) not in {"PENDING", "READY"}:
                    raise ValueError("QUEUE-005 已下載或已顯示的 Slot 不可替換")
                if not self._is_future_item(queue_item["display_after"], now):
                    raise ValueError("QUEUE-005 已到期或目前 Slot 不可替換")
                if str(slot["release_id"]) == normalized_release_id:
                    connection.execute("COMMIT")
                    return self._row(connection, schedule_id) or {}
                release = connection.execute(
                    """
                    SELECT id,manifest_json FROM releases
                    WHERE id=? AND status='published' AND render_profile=?
                    """,
                    (normalized_release_id, str(schedule["panel_profile"])),
                ).fetchone()
                if release is None:
                    raise ValueError("QUEUE-002 Release 不存在、未發布或 Profile 不相容")
                duplicate = connection.execute(
                    """
                    SELECT 1 FROM device_content_queue_items
                    WHERE device_id=? AND release_id=? AND id<>?
                      AND status IN ('PENDING','READY','AVAILABLE','DOWNLOADED','ACKNOWLEDGED')
                    LIMIT 1
                    """,
                    (device_id, normalized_release_id, slot["queue_item_id"]),
                ).fetchone()
                if duplicate is not None:
                    raise ValueError("QUEUE-005 Release 已在活動 Queue 中，不可重複佔用 Slot")
                entry = self._manifest_entry(str(release["manifest_json"]))
                connection.execute(
                    """
                    UPDATE device_content_queue_items
                    SET release_id=?,last_error_code=NULL,retry_count=0,updated_at=?
                    WHERE id=? AND device_id=? AND status IN ('PENDING','READY')
                    """,
                    (normalized_release_id, now, slot["queue_item_id"], device_id),
                )
                connection.execute(
                    """
                    UPDATE device_offline_schedule_slots
                    SET release_id=?,sha256=? WHERE id=? AND schedule_id=?
                    """,
                    (
                        normalized_release_id,
                        str(entry["sha256"]).lower(),
                        slot["id"],
                        schedule_id,
                    ),
                )
                release_rows = connection.execute(
                    """
                    SELECT release_id FROM device_offline_schedule_slots
                    WHERE schedule_id=? ORDER BY slot_index
                    """,
                    (schedule_id,),
                ).fetchall()
                configured_times = json.loads(str(schedule["schedule_times_json"] or "[]"))
                playlist_version = self.playlist_version(
                    device_id=device_id,
                    target_date=str(schedule["target_date"]),
                    config_version=int(schedule["config_version"]),
                    schedule_times=configured_times,
                    release_ids=[str(row["release_id"]) for row in release_rows],
                )
                snapshot = json.loads(str(schedule["snapshot_json"] or "{}"))
                if not isinstance(snapshot, dict):
                    snapshot = {}
                snapshot["playlist_version"] = playlist_version
                snapshot["playlist_revision"] = int(snapshot.get("playlist_revision") or 0) + 1
                connection.execute(
                    """
                    UPDATE device_offline_schedules SET snapshot_json=?,updated_at=? WHERE id=?
                    """,
                    (json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")), now, schedule_id),
                )
                connection.execute(
                    """
                    UPDATE device_content_queues
                    SET queue_version=queue_version+1,next_queued_release_id=CASE WHEN ?=0 THEN ? ELSE next_queued_release_id END,updated_at=?
                    WHERE device_id=?
                    """,
                    (slot_index, normalized_release_id, now, device_id),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        with self.database.session() as connection:
            return self._row(connection, schedule_id) or {}

    def due_prefetch_devices(
        self, *, limit: int = 32, after_device_id: str | None = None
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 128))
        with self.database.session() as connection:
            after = str(after_device_id or "")
            rows = connection.execute(
                """
                SELECT id,name,timezone,schedule_times_json,prefetch_lead_minutes,config_version,
                       minimum_schedule_gap_minutes,sync_strategy,sync_time
                FROM devices
                WHERE enabled=1 AND delivery_mode='inktime_offline_schedule'
                  AND offline_prefetch_allowed=1
                  AND id>?
                ORDER BY id LIMIT ?
                """,
                (after, bounded),
            ).fetchall()
            if not rows and after:
                rows = connection.execute(
                    """
                    SELECT id,name,timezone,schedule_times_json,prefetch_lead_minutes,config_version,
                           minimum_schedule_gap_minutes,sync_strategy,sync_time
                    FROM devices
                    WHERE enabled=1 AND delivery_mode='inktime_offline_schedule'
                      AND offline_prefetch_allowed=1
                    ORDER BY id LIMIT ?
                    """,
                    (bounded,),
                ).fetchall()
        return [dict(row) for row in rows]

    def prefetch_cursor(self) -> str | None:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT last_device_id FROM device_offline_prefetch_cursors WHERE id=1"
            ).fetchone()
        return str(row["last_device_id"]) if row and row["last_device_id"] else None

    def advance_prefetch_cursor(self, device_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO device_offline_prefetch_cursors(id,last_device_id,updated_at)
                VALUES (1,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    last_device_id=excluded.last_device_id,updated_at=excluded.updated_at
                """,
                (str(device_id), datetime.now(timezone.utc).isoformat()),
            )
