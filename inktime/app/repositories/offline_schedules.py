"""Atomic server-side preparation and device projection for offline schedules."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import json
import re
from typing import Any, Sequence
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from inktime.app.core.paths import UnsafePathError
from inktime.app.db import Database
from inktime.app.domain.photopainter.offline_schedule import slot_deadlines, validate_offline_schedule
from inktime.app.services.device_releases import payload_entry_from_manifest


_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_PREPARED_SLOTS = 12


class OfflineScheduleRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

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
            slot.update(
                {
                    "sha256": str(entry["sha256"]).lower(),
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
        device_snapshot = {
            "id": str(schedule["device_id"]),
            "panel_profile": str(schedule["panel_profile"]),
            "rotation": int(schedule["rotation"]),
            "timezone": str(schedule["timezone"]),
            "schedule_times_json": str(schedule["schedule_times_json"]),
            "prefetch_lead_minutes": int(schedule["prefetch_lead_minutes"]),
            "button_wake_action": str(schedule["button_wake_action"]),
            "offline_schedule_version": int(schedule["offline_schedule_version"]),
            "snapshot_json": snapshot_json if isinstance(snapshot_json, dict) else {},
        }
        return {
            "schedule": dict(schedule),
            "device": device_snapshot,
            "slots": normalized_slots,
        }

    def latest_for_device(self, device_id: str) -> dict[str, Any] | None:
        with self.database.session() as connection:
            row = connection.execute(
                """
                SELECT id FROM device_offline_schedules
                WHERE device_id=? AND status='ready'
                ORDER BY target_date DESC,config_version DESC,created_at DESC
                LIMIT 1
                """,
                (device_id,),
            ).fetchone()
            return self._row(connection, str(row["id"])) if row else None

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
                SELECT id FROM device_offline_schedules
                WHERE device_id=? AND target_date=? AND config_version=? AND status='ready'
                ORDER BY updated_at DESC,id DESC LIMIT 1
                """,
                (device_id, self._date(target_date).isoformat(), int(config_version)),
            ).fetchone()
            return self._row(connection, str(row["id"])) if row else None

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
            raise ValueError("DEVICE-008 一日離線排程最多 12 個 Slot")
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
                           button_wake_action,offline_schedule_version
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
                configured_times = validate_offline_schedule(configured_times, maximum=MAX_PREPARED_SLOTS)
                if len(normalized_release_ids) != len(configured_times):
                    raise ValueError("DEVICE-008 Release 數量必須等於裝置 schedule_times 數量")
                existing = connection.execute(
                    """
                    SELECT id,status FROM device_offline_schedules
                    WHERE device_id=? AND target_date=? AND config_version=?
                    """,
                    (device_id, day.isoformat(), current_config_version),
                ).fetchone()
                if existing is not None and str(existing["status"]) == "ready":
                    return self._row(connection, str(existing["id"])) or {}
                schedule_id = str(existing["id"]) if existing is not None else str(uuid4())
                snapshot = {
                    "panel_profile": str(device["panel_profile"]),
                    "rotation": int(device["rotation"]),
                    "timezone": str(device["timezone"]),
                    "schedule_times": list(configured_times),
                    "prefetch_lead_minutes": int(device["prefetch_lead_minutes"]),
                    "button_wake_action": str(device["button_wake_action"]),
                    "offline_schedule_version": int(device["offline_schedule_version"]),
                    "config_version": current_config_version,
                }
                connection.execute(
                    """
                    INSERT INTO device_offline_schedules(
                        id,device_id,target_date,config_version,timezone,status,created_at,updated_at,
                        panel_profile,rotation,schedule_times_json,prefetch_lead_minutes,
                        button_wake_action,offline_schedule_version,snapshot_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(device_id,target_date,config_version) DO UPDATE SET
                        status='preparing',updated_at=excluded.updated_at,
                        panel_profile=excluded.panel_profile,rotation=excluded.rotation,
                        schedule_times_json=excluded.schedule_times_json,
                        prefetch_lead_minutes=excluded.prefetch_lead_minutes,
                        button_wake_action=excluded.button_wake_action,
                        offline_schedule_version=excluded.offline_schedule_version,
                        snapshot_json=excluded.snapshot_json
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
                        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    ),
                )
                connection.execute(
                    "DELETE FROM device_offline_schedule_slots WHERE schedule_id=?", (schedule_id,)
                )
                # Current and future dates are independent queue owners.  A
                # retry may supersede only an earlier preparation for this
                # same local date.
                connection.execute(
                    """
                    UPDATE device_offline_schedules SET status='cancelled',updated_at=?
                    WHERE device_id=? AND target_date=? AND id<>? AND status IN ('preparing','ready')
                    """,
                    (now, device_id, day.isoformat(), schedule_id),
                )
                cancelled = connection.execute(
                    """
                    UPDATE device_content_queue_items SET status='CANCELLED',updated_at=?
                    WHERE device_id=? AND offline_schedule_id IN (
                        SELECT id FROM device_offline_schedules
                        WHERE device_id=? AND target_date=? AND id<>? AND status='cancelled'
                    ) AND status IN ('PENDING','READY','AVAILABLE','DOWNLOADED','ACKNOWLEDGED')
                    """,
                    (now, device_id, device_id, day.isoformat(), schedule_id),
                ).rowcount
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
                    "UPDATE device_offline_schedules SET status='ready',updated_at=? WHERE id=?",
                    (now, schedule_id),
                )
                current = connection.execute(
                    """
                    SELECT config_version,panel_profile,rotation,timezone,schedule_times_json,
                           prefetch_lead_minutes,button_wake_action,offline_schedule_version
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
                ):
                    raise ValueError("DISPLAY-CONFIG-RACE 裝置設定在離線排程提交前已變更")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            result = self._row(connection, schedule_id)
            if result is None:
                raise RuntimeError("DEVICE-008 離線排程準備結果不存在")
            return result

    def due_prefetch_devices(
        self, *, limit: int = 32, after_device_id: str | None = None
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 128))
        with self.database.session() as connection:
            after = str(after_device_id or "")
            rows = connection.execute(
                """
                SELECT id,name,timezone,schedule_times_json,prefetch_lead_minutes,config_version
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
                    SELECT id,name,timezone,schedule_times_json,prefetch_lead_minutes,config_version
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
