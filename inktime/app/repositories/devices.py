from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hmac
import json
import secrets
from typing import Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from inktime.app.core.security import hash_device_secret, hash_device_token, issue_device_token
from inktime.app.db import Database
from inktime.app.domain.photopainter.device_configuration import (
    classify_device_configuration_changes,
)
from inktime.app.domain.photopainter.offline_schedule import (
    LEGACY_MAX_OFFLINE_SLOTS,
    MINIMUM_SCHEDULE_GAP_MINUTES,
    OFFLINE_PREPARE_BOOTSTRAP_AT,
    offline_schedule_capability_state,
    resolve_offline_schedule_max_slots,
    normalize_delivery_contract,
    normalize_sync_strategy,
    next_sync_epoch,
    stored_schedule_state,
    validate_offline_schedule,
)


_UNSET = object()
_DEVICE_AUTH_FAILURE_LIMIT = 20
_DEVICE_AUTH_FAILURE_WINDOW = timedelta(minutes=5)
_DEVICE_AUTH_FAILURE_MAX_ROWS = 10_000
_DEVICE_AUTH_TIMESTAMP_UPDATE_INTERVAL = timedelta(minutes=1)


class DeviceRateLimitError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: int = 300) -> None:
        super().__init__(message)
        self.retry_after_seconds = max(1, min(int(retry_after_seconds), 300))


class DeviceRepository:
    def __init__(self, database: Database, pepper: str) -> None:
        self.database = database
        self.pepper = pepper

    def list(self):
        with self.database.session() as connection:
            return connection.execute(
                """
                SELECT id, name, enabled, firmware_version, timezone, schedule, rotation,
                       panel_profile, config_version, acked_config_version, config_ack_at,
                       last_seen_at, last_ip, last_download_at, last_release_id,
                       download_success_count, download_failure_count, wifi_rssi, battery_percent,
                       free_heap_bytes, free_psram_bytes, last_error_code, last_error_message,
                       last_status_at, wake_reason, offline_alert_active,
                       last_offline_alert_at, last_recovery_alert_at,
                       battery_capacity_mah, standby_current_ma, active_current_ma,
                       refreshes_per_day, battery_reserve_percent, energy_profile_updated_at,
                       delivery_mode, offline_prefetch_allowed, offline_schedule_json,
                       offline_schedule_version, applied_offline_schedule_version,
                       offline_schedule_max_slots, offline_schedule_capability_state,
                       next_offline_prepare_at,
                       offline_schedule_ack_at, last_offline_slot, schedule_times_json,
                       prefetch_lead_minutes, button_wake_action, minimum_schedule_gap_minutes,
                       sync_strategy, sync_time, stock_endpoint_host,
                       frame_orientation, layout_mode, fit_mode,
                       auth_mode, pairing_state, credential_version, paired_at, last_auth_at,
                       auth_revoked_at, repair_allowed_until, pairing_expires_at, pairing_attempts,
                       pairing_claim_attempts, pairing_requested_at, firmware_identity
                FROM devices ORDER BY name
                """
            ).fetchall()

    def get(self, device_id: str, *, connection=None):
        context = nullcontext(connection) if connection is not None else self.database.session()
        with context as active_connection:
            return active_connection.execute(
                "SELECT * FROM devices WHERE id=?", (device_id,)
            ).fetchone()

    def create(
        self,
        name: str,
        *,
        enabled: bool = True,
        timezone_name: str = "Asia/Taipei",
        schedule: str = "08:00",
        delivery_mode: str = "legacy_online",
        offline_prefetch_allowed: bool | None = None,
        offline_schedule: Sequence[str] | None = None,
        schedule_times: Sequence[str] | None = None,
        offline_schedule_max_slots: int = LEGACY_MAX_OFFLINE_SLOTS,
        prefetch_lead_minutes: int = 5,
        button_wake_action: str = "check_new",
        minimum_schedule_gap_minutes: int = MINIMUM_SCHEDULE_GAP_MINUTES,
        sync_strategy: str = "first_display_lead",
        sync_time: str | None = None,
        stock_endpoint_host: str | None = None,
        rotation: int = 0,
        panel_profile: str = "safe_4c",
        frame_orientation: str | None = None,
        layout_mode: str | None = None,
        fit_mode: str | None = None,
        auth_mode: str = "legacy_token",
    ) -> tuple[str, str | None]:
        device_id = str(uuid4())
        auth_mode = str(auth_mode).strip() or "legacy_token"
        if delivery_mode == "stock_compat":
            auth_mode = "stock"
        if auth_mode not in {"automatic", "legacy_token", "stock"}:
            raise ValueError("DEVICE-011 auth_mode 不合法")
        token = issue_device_token() if auth_mode in {"legacy_token", "stock"} else None
        # The legacy schema requires token_hash to be non-null.  An explicitly
        # created automatic scaffold receives an unreachable random placeholder
        # and remains disabled until the authenticated confirm step.
        token_for_storage = token or ("auto-placeholder-" + secrets.token_urlsafe(32))
        pairing_state = "unpaired" if auth_mode == "automatic" else "paired"
        if auth_mode == "automatic":
            # New custom devices must be created by ESP32 enrollment and only
            # become enabled after the authenticated confirm step.
            enabled = False
        now = datetime.now(timezone.utc).isoformat()
        delivery_mode, offline_prefetch_allowed = normalize_delivery_contract(
            delivery_mode,
            offline_prefetch_allowed,
            explicit_prefetch=offline_prefetch_allowed is not None,
        )
        if type(minimum_schedule_gap_minutes) is not int or not 30 <= minimum_schedule_gap_minutes <= 360:
            raise ValueError("DEVICE-008 minimum_schedule_gap_minutes 必須介於 30 到 360")
        maximum_slots = resolve_offline_schedule_max_slots(
            {"offline_schedule_max_slots": offline_schedule_max_slots}
        )
        schedule_values = validate_offline_schedule(
            schedule_times or offline_schedule or [schedule],
            maximum=maximum_slots,
            minimum_gap_minutes=minimum_schedule_gap_minutes,
        )
        if not 0 <= int(prefetch_lead_minutes) <= 120:
            raise ValueError("DEVICE-008 prefetch_lead_minutes 必須介於 0 到 120")
        if button_wake_action not in {"check_new", "local_next"}:
            raise ValueError("DEVICE-008 button_wake_action 不合法")
        sync_strategy, sync_time = normalize_sync_strategy(sync_strategy, sync_time)
        with self.database.session() as connection:
            connection.execute(
                """
                INSERT INTO devices(
                    id, name, token_hash, enabled, timezone, schedule, rotation, panel_profile,
                    frame_orientation, layout_mode, fit_mode, delivery_mode,
                    offline_prefetch_allowed, offline_schedule_json, offline_schedule_version,
                    offline_schedule_max_slots, offline_schedule_capability_state, next_offline_prepare_at,
                    schedule_times_json, prefetch_lead_minutes, button_wake_action,
                    minimum_schedule_gap_minutes, sync_strategy, sync_time, stock_endpoint_host,
                    auth_mode, pairing_state, credential_version,
                    created_at, updated_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    device_id,
                    name.strip(),
                    hash_device_token(token_for_storage, self.pepper),
                    int(enabled),
                    timezone_name,
                    schedule,
                    rotation,
                    panel_profile,
                    frame_orientation,
                    layout_mode,
                    fit_mode,
                    delivery_mode,
                    int(offline_prefetch_allowed),
                    json.dumps(schedule_values, ensure_ascii=False),
                    1 if delivery_mode == "inktime_offline_schedule" else 0,
                    maximum_slots,
                    offline_schedule_capability_state(maximum_slots),
                    OFFLINE_PREPARE_BOOTSTRAP_AT if offline_prefetch_allowed and enabled else None,
                    json.dumps(schedule_values, ensure_ascii=False),
                    int(prefetch_lead_minutes),
                    button_wake_action,
                    int(minimum_schedule_gap_minutes),
                    sync_strategy,
                    sync_time,
                    stock_endpoint_host,
                    auth_mode,
                    pairing_state,
                    0,
                    now,
                    now,
                ),
            )
        return device_id, token

    def regenerate(self, device_id: str) -> str:
        token = issue_device_token()
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            current = connection.execute(
                "SELECT auth_mode FROM devices WHERE id=?", (device_id,)
            ).fetchone()
            if current is None:
                raise KeyError(device_id)
            if str(current["auth_mode"] or "legacy_token") == "automatic":
                raise ValueError("DEVICE-011 自動配對裝置不支援手動 Token")
            cursor = connection.execute(
                "UPDATE devices SET token_hash=?, auth_mode=CASE WHEN auth_mode='stock' THEN 'stock' ELSE 'legacy_token' END, updated_at=? WHERE id=?",
                (hash_device_token(token, self.pepper), now, device_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(device_id)
        return token

    @staticmethod
    def _is_future_queue_slot(value: object, now: str) -> bool:
        if not value:
            return False
        try:
            slot_at = datetime.fromisoformat(str(value))
            authoritative_now = datetime.fromisoformat(str(now))
        except (TypeError, ValueError):
            return False
        if slot_at.tzinfo is None:
            slot_at = slot_at.replace(tzinfo=timezone.utc)
        if authoritative_now.tzinfo is None:
            authoritative_now = authoritative_now.replace(tzinfo=timezone.utc)
        return slot_at.astimezone(timezone.utc) > authoritative_now.astimezone(timezone.utc)

    def _supersede_future_queue_items(
        self,
        connection,
        *,
        device_id: str,
        delivery_modes: Sequence[str],
        now: str,
        old_delivery_mode: str,
        new_delivery_mode: str,
        change_kind: str,
    ) -> int:
        if not delivery_modes:
            return 0
        placeholders = ",".join("?" for _ in delivery_modes)
        items = connection.execute(
            f"SELECT id,status,delivery_mode,display_after,offline_slot,offline_schedule_id "  # noqa: S608
            f"FROM device_content_queue_items WHERE device_id=? AND delivery_mode IN ({placeholders}) "
            "AND status NOT IN ('DISPLAYED','FAILED','EXPIRED','CANCELLED') ORDER BY id",
            (device_id, *delivery_modes),
        ).fetchall()
        cancelled = 0
        for item in items:
            if not self._is_future_queue_slot(item["display_after"], now):
                # Immutable history includes a past slot even if the device
                # never reached it.  Only a genuinely future point may be
                # replanned by a new device configuration.
                continue
            item_id = str(item["id"])
            connection.execute(
                "UPDATE device_content_queue_items SET status='CANCELLED',last_error_code='QUEUE-005',updated_at=? WHERE id=?",
                (now, item_id),
            )
            event_type = (
                "DELIVERY_MODE_TRANSITION_CANCELLED"
                if change_kind == "delivery_mode_transition"
                else "FUTURE_SLOT_SUPERSEDED"
            )
            payload = {
                "reason": (
                    "delivery_mode_transition"
                    if change_kind == "delivery_mode_transition"
                    else change_kind
                ),
                "change_kind": change_kind,
                "old_delivery_mode": old_delivery_mode,
                "new_delivery_mode": new_delivery_mode,
                "queue_item_delivery_mode": str(item["delivery_mode"]),
                "offline_slot": item["offline_slot"],
                "offline_schedule_id": item["offline_schedule_id"],
            }
            connection.execute(
                "INSERT OR IGNORE INTO device_content_queue_events(queue_item_id,device_id,event_type,idempotency_key,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                (
                    item_id,
                    device_id,
                    event_type,
                    f"{event_type}:{device_id}:{item_id}",
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
            connection.execute(
                "UPDATE rollout_targets SET status='cancelled_future_schedule',last_error_code='QUEUE-005',updated_at=? WHERE queue_item_id=?",
                (now, item_id),
            )
            cancelled += 1
        if cancelled:
            connection.execute(
                "UPDATE device_content_queues SET queue_version=queue_version+?,updated_at=? WHERE device_id=?",
                (cancelled, now, device_id),
            )
        return cancelled

    @staticmethod
    def _record_past_schedule_slots(
        connection,
        *,
        device_id: str,
        timezone_name: str,
        old_schedule: Sequence[str],
        new_schedule: Sequence[str],
        now: str,
        config_version: int,
    ) -> None:
        try:
            zone = ZoneInfo(timezone_name)
            authoritative_now = datetime.fromisoformat(now).astimezone(zone)
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            return
        old_values = {str(value) for value in old_schedule}
        for slot in new_schedule:
            normalized_slot = str(slot)
            if normalized_slot in old_values:
                continue
            hour, minute = (int(part) for part in normalized_slot.split(":"))
            slot_at = datetime.combine(
                authoritative_now.date(),
                datetime.min.time().replace(hour=hour, minute=minute),
                tzinfo=zone,
            )
            if slot_at > authoritative_now:
                continue
            details = {
                "planning_trace": "past_slot_not_replayed",
                "slot": normalized_slot,
                "target_local_date": authoritative_now.date().isoformat(),
                "authoritative_now": now,
                "config_version": config_version,
                "reason": "device_schedule_changed",
            }
            connection.execute(
                """
                INSERT INTO device_events(
                    device_id,level,event,error_code,message,details_json,created_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    device_id,
                    "info",
                    "schedule_replan_skip",
                    "SCHEDULE-PAST-SKIP",
                    "新排程的過去 Slot 已略過，不重播歷史時刻",
                    json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )

    def update(
        self,
        device_id: str,
        *,
        name: str,
        enabled: bool,
        timezone_name: str,
        schedule: str,
        delivery_mode: str = "legacy_online",
        offline_prefetch_allowed: bool | None = None,
        offline_schedule: Sequence[str] | None = None,
        schedule_times: Sequence[str] | None = None,
        explicit_schedule_replacement: bool | None = None,
        prefetch_lead_minutes: int = 5,
        button_wake_action: str = "check_new",
        minimum_schedule_gap_minutes: int = MINIMUM_SCHEDULE_GAP_MINUTES,
        sync_strategy: str = "first_display_lead",
        sync_time: str | None = None,
        stock_endpoint_host: str | None = None,
        rotation: int,
        panel_profile: str,
        frame_orientation: str | None | object = _UNSET,
        layout_mode: str | None | object = _UNSET,
        fit_mode: str | None | object = _UNSET,
        connection=None,
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        context = nullcontext(connection) if connection is not None else self.database.session()
        with context as connection:
            nested = connection.in_transaction
            if nested:
                connection.execute("SAVEPOINT device_update")
            else:
                connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    """
                    SELECT enabled,timezone,schedule,rotation,panel_profile,delivery_mode,offline_prefetch_allowed,
                           offline_schedule_json,schedule_times_json,prefetch_lead_minutes,
                           offline_schedule_max_slots,offline_schedule_capability_state,
                           next_offline_prepare_at,
                           button_wake_action,minimum_schedule_gap_minutes,sync_strategy,sync_time,
                           stock_endpoint_host,frame_orientation,layout_mode,fit_mode,
                           auth_mode,pairing_state,config_version,name
                    FROM devices WHERE id=?
                    """,
                    (device_id,),
                ).fetchone()
                if current is None:
                    raise KeyError(device_id)
                selected_orientation = (
                    current["frame_orientation"] if frame_orientation is _UNSET else frame_orientation
                )
                selected_layout = current["layout_mode"] if layout_mode is _UNSET else layout_mode
                selected_fit = current["fit_mode"] if fit_mode is _UNSET else fit_mode
                delivery_mode, offline_prefetch_allowed = normalize_delivery_contract(
                    delivery_mode,
                    offline_prefetch_allowed,
                    explicit_prefetch=offline_prefetch_allowed is not None,
                )
                current_auth_mode = str(current["auth_mode"] or "legacy_token")
                if delivery_mode == "stock_compat" and current_auth_mode == "automatic":
                    raise ValueError(
                        "DEVICE-011 自動配對裝置不可直接切換 Stock 相容模式；請建立獨立 Stock 裝置"
                    )
                selected_auth_mode = "stock" if delivery_mode == "stock_compat" else (
                    "legacy_token" if current_auth_mode == "stock" else current_auth_mode
                )
                selected_pairing_state = "paired" if selected_auth_mode != "automatic" else str(
                    current["pairing_state"] or "unpaired"
                )
                schedule_times_state = stored_schedule_state(current["schedule_times_json"])
                offline_schedule_state = stored_schedule_state(current["offline_schedule_json"])
                if schedule_times_state.is_array and schedule_times_state.values:
                    existing_schedule = schedule_times_state.values
                elif offline_schedule_state.is_array and offline_schedule_state.values:
                    existing_schedule = offline_schedule_state.values
                else:
                    existing_schedule = []
                explicit_schedule = (
                    schedule_times if schedule_times is not None else offline_schedule
                )
                inferred_schedule_replacement = bool(
                    (
                        schedule_times is not None
                        and (
                            not schedule_times_state.is_array
                            or list(schedule_times) != schedule_times_state.values
                        )
                    )
                    or (
                        schedule_times is None
                        and offline_schedule is not None
                        and (
                            not offline_schedule_state.is_array
                            or list(offline_schedule) != offline_schedule_state.values
                        )
                    )
                )
                explicit_schedule_changed = (
                    inferred_schedule_replacement
                    if explicit_schedule_replacement is None
                    else bool(explicit_schedule_replacement)
                )
                if explicit_schedule is not None:
                    selected_schedule = explicit_schedule
                else:
                    selected_schedule = existing_schedule or [schedule]
                if explicit_schedule is None and len(existing_schedule) == 1:
                    selected_schedule = [schedule]
                if type(minimum_schedule_gap_minutes) is not int or not 30 <= minimum_schedule_gap_minutes <= 360:
                    raise ValueError("DEVICE-008 minimum_schedule_gap_minutes 必須介於 30 到 360")
                maximum_slots = resolve_offline_schedule_max_slots(
                    {"offline_schedule_max_slots": current["offline_schedule_max_slots"]}
                )
                schedule_was_modified = bool(
                    explicit_schedule_changed
                    or str(current["schedule"]) != schedule
                    or int(
                        current["minimum_schedule_gap_minutes"]
                        or MINIMUM_SCHEDULE_GAP_MINUTES
                    )
                    != int(minimum_schedule_gap_minutes)
                )
                preserve_quarantined_schedule = bool(
                    str(current["offline_schedule_capability_state"] or "")
                    == "legacy_ambiguous"
                    and not schedule_was_modified
                    and (
                        not enabled
                        or delivery_mode != "inktime_offline_schedule"
                    )
                )
                if (
                    str(current["offline_schedule_capability_state"] or "")
                    == "legacy_ambiguous"
                    and not schedule_was_modified
                    and not preserve_quarantined_schedule
                ):
                    raise ValueError(
                        "DEVICE-008 隔離中的離線排程必須先停用、切離離線模式，或明確提交有效替代排程"
                    )
                if preserve_quarantined_schedule:
                    schedule_values = existing_schedule
                    stored_schedule = str(current["schedule"])
                    stored_offline_schedule = offline_schedule_state.raw
                    stored_schedule_times = schedule_times_state.raw
                else:
                    schedule_values = validate_offline_schedule(
                        selected_schedule,
                        maximum=maximum_slots,
                        minimum_gap_minutes=minimum_schedule_gap_minutes,
                    )
                    stored_schedule = schedule_values[0]
                    stored_offline_schedule = json.dumps(schedule_values, ensure_ascii=False)
                    stored_schedule_times = json.dumps(schedule_values, ensure_ascii=False)
                if not 0 <= int(prefetch_lead_minutes) <= 120:
                    raise ValueError("DEVICE-008 prefetch_lead_minutes 必須介於 0 到 120")
                if button_wake_action not in {"check_new", "local_next"}:
                    raise ValueError("DEVICE-008 button_wake_action 不合法")
                sync_strategy, sync_time = normalize_sync_strategy(sync_strategy, sync_time)
                changes = classify_device_configuration_changes(
                    {
                        "timezone": str(current["timezone"]),
                        "rotation": int(current["rotation"]),
                        "panel_profile": str(current["panel_profile"]),
                        "delivery_mode": str(current["delivery_mode"]),
                        "offline_prefetch_allowed": bool(current["offline_prefetch_allowed"]),
                        "enabled": bool(current["enabled"]),
                        "prefetch_lead_minutes": int(current["prefetch_lead_minutes"]),
                        "button_wake_action": str(current["button_wake_action"]),
                        "minimum_schedule_gap_minutes": int(
                            current["minimum_schedule_gap_minutes"]
                            or MINIMUM_SCHEDULE_GAP_MINUTES
                        ),
                        "sync_strategy": str(current["sync_strategy"] or "first_display_lead"),
                        "sync_time": current["sync_time"] or None,
                        "stock_endpoint_host": current["stock_endpoint_host"] or None,
                        "frame_orientation": current["frame_orientation"],
                        "layout_mode": current["layout_mode"],
                        "fit_mode": current["fit_mode"],
                    },
                    {
                        "timezone": timezone_name,
                        "rotation": rotation,
                        "panel_profile": panel_profile,
                        "delivery_mode": delivery_mode,
                        "offline_prefetch_allowed": bool(offline_prefetch_allowed),
                        "enabled": bool(enabled),
                        "prefetch_lead_minutes": int(prefetch_lead_minutes),
                        "button_wake_action": button_wake_action,
                        "minimum_schedule_gap_minutes": int(minimum_schedule_gap_minutes),
                        "sync_strategy": sync_strategy,
                        "sync_time": sync_time or None,
                        "stock_endpoint_host": stock_endpoint_host or None,
                        "frame_orientation": selected_orientation,
                        "layout_mode": selected_layout,
                        "fit_mode": selected_fit,
                    },
                    schedule_definition_changed=schedule_was_modified,
                )
                remote_changed = changes.remote_config_changed
                offline_schedule_changed = bool(
                    changes.offline_schedule_changed and not preserve_quarantined_schedule
                )
                if not remote_changed and str(current["name"]) == name.strip():
                    if nested:
                        connection.execute("RELEASE SAVEPOINT device_update")
                    else:
                        connection.execute("COMMIT")
                    return False
                cursor = connection.execute(
                    """
                    UPDATE devices
                    SET name=?,enabled=?,timezone=?,schedule=?,rotation=?,panel_profile=?,
                        delivery_mode=?,offline_prefetch_allowed=?,offline_schedule_json=?,schedule_times_json=?,
                        prefetch_lead_minutes=?,button_wake_action=?,minimum_schedule_gap_minutes=?,
                        sync_strategy=?,sync_time=?,stock_endpoint_host=?,
                        offline_schedule_version=offline_schedule_version+CASE WHEN ? THEN 1 ELSE 0 END,
                        next_offline_prepare_at=CASE
                            WHEN ?=0 OR ?=0 THEN NULL
                            WHEN ?=1 THEN ?
                            ELSE next_offline_prepare_at
                        END,
                        frame_orientation=?,layout_mode=?,fit_mode=?,auth_mode=?,pairing_state=?,
                        config_version=config_version+?,updated_at=?
                    WHERE id=?
                    """,
                    (
                        name.strip(),
                        int(enabled),
                        timezone_name,
                        stored_schedule,
                        rotation,
                        panel_profile,
                        delivery_mode,
                        int(offline_prefetch_allowed),
                        stored_offline_schedule,
                        stored_schedule_times,
                        int(prefetch_lead_minutes),
                        button_wake_action,
                        int(minimum_schedule_gap_minutes),
                        sync_strategy,
                        sync_time,
                        stock_endpoint_host,
                        int(offline_schedule_changed),
                        int(delivery_mode == "inktime_offline_schedule"),
                        int(enabled),
                        int(remote_changed),
                        OFFLINE_PREPARE_BOOTSTRAP_AT,
                        selected_orientation,
                        selected_layout,
                        selected_fit,
                        selected_auth_mode,
                        selected_pairing_state,
                        int(remote_changed),
                        now,
                        device_id,
                    ),
                )
                incompatible_modes: list[str] = []
                if remote_changed:
                    if delivery_mode == "inktime_offline_schedule":
                        incompatible_modes.append("online_queue")
                        # A changed Enhanced configuration invalidates any
                        # already prepared slot snapshot; the next scheduler
                        # job must prepare the new config version instead.
                        if str(current["delivery_mode"]) == delivery_mode:
                            incompatible_modes.append("offline_schedule")
                    else:
                        incompatible_modes.append("offline_schedule")
                if incompatible_modes:
                    self._supersede_future_queue_items(
                        connection,
                        device_id=device_id,
                        delivery_modes=incompatible_modes,
                        now=now,
                        old_delivery_mode=str(current["delivery_mode"]),
                        new_delivery_mode=delivery_mode,
                        change_kind=(
                            "delivery_mode_transition"
                            if str(current["delivery_mode"]) != delivery_mode
                            else "device_render_changed"
                            if changes.render_inputs_changed
                            and not changes.offline_schedule_changed
                            else "device_schedule_changed"
                        ),
                    )
                if (
                    delivery_mode == "inktime_offline_schedule"
                    and remote_changed
                    and schedule_was_modified
                ):
                    self._record_past_schedule_slots(
                        connection,
                        device_id=device_id,
                        timezone_name=timezone_name,
                        old_schedule=existing_schedule,
                        new_schedule=schedule_values,
                        now=now,
                        config_version=int(current["config_version"]) + 1,
                    )
                if nested:
                    connection.execute("RELEASE SAVEPOINT device_update")
                else:
                    connection.execute("COMMIT")
            except Exception:
                if nested:
                    connection.execute("ROLLBACK TO SAVEPOINT device_update")
                    connection.execute("RELEASE SAVEPOINT device_update")
                else:
                    connection.execute("ROLLBACK")
                raise
        if cursor.rowcount != 1:
            raise KeyError(device_id)
        return True

    def update_render_inputs(
        self,
        device_id: str,
        *,
        panel_profile: str | object = _UNSET,
        frame_orientation: str | None | object = _UNSET,
        layout_mode: str | None | object = _UNSET,
        fit_mode: str | None | object = _UNSET,
    ) -> bool:
        """Apply render inputs through the canonical versioning path."""

        with self.database.transaction() as connection:
            return self.update_render_inputs_in_transaction(
                connection,
                device_id,
                panel_profile=panel_profile,
                frame_orientation=frame_orientation,
                layout_mode=layout_mode,
                fit_mode=fit_mode,
            )

    def update_render_inputs_in_transaction(
        self,
        connection,
        device_id: str,
        *,
        panel_profile: str | object = _UNSET,
        frame_orientation: str | None | object = _UNSET,
        layout_mode: str | None | object = _UNSET,
        fit_mode: str | None | object = _UNSET,
    ) -> bool:
        """Apply render inputs without owning the caller's transaction."""

        current = self.get(device_id, connection=connection)
        if current is None:
            raise KeyError(device_id)
        return self.update(
            device_id,
            name=str(current["name"]),
            enabled=bool(current["enabled"]),
            timezone_name=str(current["timezone"]),
            schedule=str(current["schedule"]),
            delivery_mode=str(current["delivery_mode"]),
            offline_prefetch_allowed=bool(current["offline_prefetch_allowed"]),
            explicit_schedule_replacement=False,
            prefetch_lead_minutes=int(current["prefetch_lead_minutes"] or 0),
            button_wake_action=str(current["button_wake_action"] or "check_new"),
            minimum_schedule_gap_minutes=int(
                current["minimum_schedule_gap_minutes"] or MINIMUM_SCHEDULE_GAP_MINUTES
            ),
            sync_strategy=str(current["sync_strategy"] or "first_display_lead"),
            sync_time=current["sync_time"],
            stock_endpoint_host=current["stock_endpoint_host"],
            rotation=int(current["rotation"]),
            panel_profile=(
                str(current["panel_profile"])
                if panel_profile is _UNSET
                else str(panel_profile)
            ),
            frame_orientation=frame_orientation,
            layout_mode=layout_mode,
            fit_mode=fit_mode,
            connection=connection,
        )

    def authenticate(
        self,
        token: str,
        ip_address: str,
        credential_version: int | None = None,
        *,
        allow_repair: bool = False,
    ):
        legacy_digest = hash_device_token(token, self.pepper)
        secret_digest = hash_device_secret(token, self.pepper)
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        cutoff = (now_dt - _DEVICE_AUTH_FAILURE_WINDOW).isoformat()
        with self.database.transaction() as connection:
            candidates = connection.execute(
                """
                SELECT * FROM devices
                WHERE enabled=1 AND (
                    token_hash IN (?,?) OR device_secret_hash IN (?,?)
                )
                ORDER BY id
                """,
                (legacy_digest, secret_digest, legacy_digest, secret_digest),
            ).fetchall()
            row = None
            for candidate in candidates:
                mode = str(candidate["auth_mode"] or "legacy_token")
                if not allow_repair and mode in {"legacy_token", "stock"} and hmac.compare_digest(
                    str(candidate["token_hash"] or ""), legacy_digest
                ):
                    row = candidate
                    break
                if mode != "automatic":
                    continue
                pairing_state = str(candidate["pairing_state"] or "")
                repair_authorized = (
                    allow_repair
                    and pairing_state == "pairing_pending"
                    and bool(candidate["auth_revoked_at"])
                    and self._repair_permission_active(candidate["repair_allowed_until"], now_dt)
                )
                if allow_repair and not repair_authorized:
                    continue
                if pairing_state != "paired" and not repair_authorized:
                    continue
                current_match = bool(candidate["device_secret_hash"]) and (
                    hmac.compare_digest(str(candidate["device_secret_hash"]), secret_digest)
                    or hmac.compare_digest(str(candidate["device_secret_hash"]), legacy_digest)
                )
                if not current_match:
                    continue
                if repair_authorized:
                    if not current_match or credential_version is None:
                        continue
                    if credential_version != int(candidate["credential_version"] or 0):
                        continue
                    row = candidate
                    break
                # Automatic credentials are always paired with an explicit
                # monotonic version header.  Legacy Token clients remain
                # headerless for compatibility, but a missing or stale
                # version must not silently authenticate a rotated Secret.
                if credential_version is None:
                    continue
                expected_version = int(candidate["credential_version"] or 0)
                if credential_version != expected_version:
                    continue
                row = candidate
                break
            if row is not None:
                last_auth_at = str(row["last_auth_at"] or "")
                auth_update_due = True
                if last_auth_at:
                    try:
                        auth_update_due = (
                            now_dt - datetime.fromisoformat(last_auth_at)
                        ) >= _DEVICE_AUTH_TIMESTAMP_UPDATE_INTERVAL
                    except ValueError:
                        auth_update_due = True
                connection.execute(
                    """
                    UPDATE devices SET last_seen_at=?, last_ip=?,
                        last_auth_at=CASE WHEN ? THEN ? ELSE last_auth_at END,
                        updated_at=? WHERE id=?
                    """,
                    (now, ip_address[:64], int(auth_update_due), now, now, row["id"]),
                )
                return row

            ip_hash = hash_device_token(ip_address[:64], self.pepper)
            connection.execute("DELETE FROM device_auth_failures WHERE attempted_at<?", (cutoff,))
            failure_state = connection.execute(
                """
                    SELECT COUNT(*) AS attempts, MIN(attempted_at) AS earliest
                    FROM device_auth_failures
                    WHERE ip_hash=? AND attempted_at>=?
                    """,
                (ip_hash, cutoff),
            ).fetchone()
            if int(failure_state["attempts"]) >= _DEVICE_AUTH_FAILURE_LIMIT:
                earliest = datetime.fromisoformat(str(failure_state["earliest"]))
                retry_after = max(
                    1,
                    int(((earliest + _DEVICE_AUTH_FAILURE_WINDOW) - now_dt).total_seconds()) + 1,
                )
                raise DeviceRateLimitError(
                    "DEVICE-007 裝置驗證嘗試過多",
                    retry_after_seconds=retry_after,
                )
            connection.execute(
                "INSERT INTO device_auth_failures(ip_hash,attempted_at) VALUES (?,?)",
                (ip_hash, now),
            )
            connection.execute(
                """
                DELETE FROM device_auth_failures
                WHERE id IN (
                    SELECT id FROM device_auth_failures
                    ORDER BY attempted_at DESC,id DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (_DEVICE_AUTH_FAILURE_MAX_ROWS,),
            )
        return None

    @staticmethod
    def _repair_permission_active(value, now: datetime) -> bool:
        try:
            return bool(value) and datetime.fromisoformat(str(value)) > now
        except (TypeError, ValueError):
            return False

    def record_download(self, device_id: str, release_id: str, succeeded: bool) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute(
                """
                UPDATE devices SET
                    download_success_count=download_success_count+CASE WHEN ? THEN 1 ELSE 0 END,
                    download_failure_count=download_failure_count+CASE WHEN ? THEN 0 ELSE 1 END,
                    last_download_at=?,
                    last_release_id=CASE WHEN ? THEN ? ELSE last_release_id END, updated_at=?
                WHERE id=?
                """,
                (int(succeeded), int(succeeded), now, int(succeeded), release_id, now, device_id),
            )

    def record_stock_upload_event(
        self,
        device_id: str,
        *,
        release_id: str,
        file_name: str,
        payload_bytes: int,
        status_code: int | None,
        upload_accepted: bool,
        error_code: str | None = None,
    ) -> None:
        """Record bounded Stock transport telemetry without payload/path data."""

        now = datetime.now(timezone.utc).isoformat()
        details = {
            "release_id": str(release_id)[:128],
            "file_name": str(file_name)[:128],
            "payload_bytes": max(0, min(int(payload_bytes), 2_147_483_647)),
            "status_code": None if status_code is None else max(100, min(int(status_code), 599)),
            "upload_accepted": bool(upload_accepted),
            "display_completed": False,
        }
        with self.database.session() as connection:
            connection.execute(
                """
                INSERT INTO device_events(
                    device_id,level,event,error_code,message,details_json,created_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    device_id,
                    "info" if upload_accepted else "warning",
                    "stock_upload",
                    str(error_code or "")[:64] or None,
                    "Stock Payload 已上傳" if upload_accepted else "Stock Payload 上傳未被接受",
                    json.dumps(details, ensure_ascii=False),
                    now,
                ),
            )

    def record_status(
        self,
        device_id: str,
        *,
        firmware_version: str,
        wifi_rssi: int | None,
        battery_percent: float | None,
        free_heap_bytes: int | None,
        free_psram_bytes: int | None,
        error_code: str,
        error_message: str,
        wake_reason: str,
        applied_config_version: int | None = None,
        applied_offline_schedule_version: int | None = None,
        details: dict | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        level = "error" if error_code else "info"
        message = error_message[:500] if error_message else "裝置狀態正常"
        details = details or {}
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE devices SET firmware_version=?,wifi_rssi=?,battery_percent=?,
                        free_heap_bytes=?,free_psram_bytes=?,last_error_code=?,last_error_message=?,
                        last_status_at=?,wake_reason=?,
                        acked_config_version=CASE
                            WHEN ? IS NOT NULL AND ? > acked_config_version AND ? <= config_version THEN ?
                            ELSE acked_config_version END,
                        config_ack_at=CASE
                            WHEN ? IS NOT NULL AND ? > acked_config_version AND ? <= config_version THEN ?
                            ELSE config_ack_at END,
                        applied_offline_schedule_version=CASE
                            WHEN ? IS NOT NULL AND ? > applied_offline_schedule_version
                                 AND ? <= offline_schedule_version THEN ?
                            ELSE applied_offline_schedule_version END,
                        offline_schedule_ack_at=CASE
                            WHEN ? IS NOT NULL AND ? > applied_offline_schedule_version
                                 AND ? <= offline_schedule_version THEN ?
                            ELSE offline_schedule_ack_at END,
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        firmware_version[:64] or None,
                        wifi_rssi,
                        battery_percent,
                        free_heap_bytes,
                        free_psram_bytes,
                        error_code[:64] or None,
                        error_message[:500] or None,
                        now,
                        wake_reason[:64] or None,
                        # acked_config_version CASE
                        applied_config_version,
                        applied_config_version,
                        applied_config_version,
                        applied_config_version,
                        # config_ack_at CASE
                        applied_config_version,
                        applied_config_version,
                        applied_config_version,
                        applied_config_version,
                        # applied_offline_schedule_version CASE
                        applied_offline_schedule_version,
                        applied_offline_schedule_version,
                        applied_offline_schedule_version,
                        applied_offline_schedule_version,
                        # offline_schedule_ack_at CASE
                        applied_offline_schedule_version,
                        applied_offline_schedule_version,
                        applied_offline_schedule_version,
                        applied_offline_schedule_version,
                        now,
                        device_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO device_events(device_id,level,event,error_code,message,details_json,created_at)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        device_id,
                        level,
                        "status_report",
                        error_code[:64] or None,
                        message,
                        json.dumps(details, ensure_ascii=False),
                        now,
                    ),
                )
                energy_values = (
                    details.get("battery_voltage"),
                    battery_percent,
                    details.get("usb_power"),
                    details.get("last_refresh_duration_ms"),
                    details.get("wake_duration_ms"),
                    details.get("temperature_c"),
                    details.get("humidity_percent"),
                )
                if any(value is not None for value in energy_values):
                    estimated = details.get("battery_percent_estimated")
                    usb_power = details.get("usb_power")
                    connection.execute(
                        """
                        INSERT INTO device_power_samples(
                            device_id,battery_voltage,battery_percent,battery_percent_estimated,
                            usb_power,refresh_duration_ms,wake_duration_ms,display_updated,
                            temperature_c,humidity_percent,wifi_rssi,wake_reason,recorded_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            device_id,
                            details.get("battery_voltage"),
                            battery_percent,
                            None if estimated is None else int(bool(estimated)),
                            None if usb_power is None else int(bool(usb_power)),
                            details.get("last_refresh_duration_ms"),
                            details.get("wake_duration_ms"),
                            int(bool(details.get("display_updated", False))),
                            details.get("temperature_c"),
                            details.get("humidity_percent"),
                            wifi_rssi,
                            wake_reason[:64] or None,
                            now,
                        ),
                    )
                    connection.execute(
                        "DELETE FROM device_power_samples WHERE device_id=? AND recorded_at<?",
                        (device_id, cutoff),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def runtime_summary(self, device_id: str) -> dict:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        with self.database.session() as connection:
            device = connection.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if device is None:
                raise KeyError(device_id)
            try:
                zone = ZoneInfo(str(device["timezone"]))
                local_now = now.astimezone(zone)
                today = local_now.date().isoformat()
            except (TypeError, ValueError, ZoneInfoNotFoundError):
                zone = ZoneInfo("UTC")
                local_now = now
                today = local_now.date().isoformat()

            def schedule_identity(row) -> dict | None:
                if row is None:
                    return None
                return {
                    "id": str(row["id"]),
                    "target_date": str(row["target_date"]),
                    "config_version": int(row["config_version"]),
                    "offline_schedule_version": int(row["offline_schedule_version"] or 0),
                    "status": str(row["status"]),
                    "timezone": str(row["timezone"]),
                    "sync_strategy": str(row["sync_strategy"] or "first_display_lead"),
                    "sync_time": row["sync_time"],
                }

            active_row = connection.execute(
                """
                SELECT id,target_date,config_version,offline_schedule_version,status,timezone,
                       sync_strategy,sync_time
                FROM device_offline_schedules
                WHERE device_id=? AND target_date=? AND status='ready'
                ORDER BY CASE WHEN config_version=? THEN 0 ELSE 1 END,
                         updated_at DESC,id DESC LIMIT 1
                """,
                (device_id, today, int(device["config_version"])),
            ).fetchone()
            staged_row = connection.execute(
                """
                SELECT id,target_date,config_version,offline_schedule_version,status,timezone,
                       sync_strategy,sync_time
                FROM device_offline_schedules
                WHERE device_id=? AND target_date>? AND status='ready'
                  AND config_version=?
                ORDER BY target_date ASC,updated_at DESC,id DESC LIMIT 1
                """,
                (device_id, today, int(device["config_version"])),
            ).fetchone()
            timeline_rows = connection.execute(
                """
                SELECT s.id AS slot_id,s.slot_index,s.show_at,s.release_id,s.queue_item_id,
                       qi.status AS queue_status,os.id AS schedule_id,os.status AS schedule_status,
                       os.target_date,os.config_version,os.offline_schedule_version
                FROM device_offline_schedule_slots s
                JOIN device_offline_schedules os ON os.id=s.schedule_id
                LEFT JOIN device_content_queue_items qi ON qi.id=s.queue_item_id
                WHERE os.device_id=? AND os.target_date=?
                ORDER BY s.show_at ASC,s.slot_index ASC
                """,
                (device_id, today),
            ).fetchall()
            today_timeline: list[dict] = []
            for row in timeline_rows:
                try:
                    show_at = datetime.fromisoformat(str(row["show_at"]))
                    if show_at.tzinfo is None:
                        show_at = show_at.replace(tzinfo=timezone.utc)
                    show_at_epoch = int(show_at.timestamp())
                except (TypeError, ValueError, OverflowError):
                    show_at_epoch = None
                today_timeline.append(
                    {
                        "slot_id": str(row["slot_id"]),
                        "slot_index": int(row["slot_index"]),
                        "show_at": str(row["show_at"]),
                        "show_at_epoch": show_at_epoch,
                        "release_id": str(row["release_id"]),
                        "queue_item_id": str(row["queue_item_id"]),
                        "queue_status": row["queue_status"],
                        "schedule_id": str(row["schedule_id"]),
                        "schedule_status": str(row["schedule_status"]),
                        "config_version": int(row["config_version"]),
                        "offline_schedule_version": int(row["offline_schedule_version"] or 0),
                    }
                )
            next_row = connection.execute(
                """
                SELECT s.id AS slot_id,s.slot_index,s.show_at,s.release_id,s.queue_item_id,
                       qi.status AS queue_status,os.id AS schedule_id,os.status AS schedule_status,
                       os.target_date,os.config_version,os.offline_schedule_version
                FROM device_offline_schedule_slots s
                JOIN device_offline_schedules os ON os.id=s.schedule_id
                JOIN device_content_queue_items qi ON qi.id=s.queue_item_id
                WHERE os.device_id=? AND s.show_at>? AND qi.status NOT IN
                      ('DISPLAYED','FAILED','EXPIRED','CANCELLED')
                ORDER BY s.show_at ASC,s.slot_index ASC LIMIT 1
                """,
                (device_id, now_iso),
            ).fetchone()

            next_display_slot = None
            if next_row is not None:
                next_display_slot = {
                    "slot_id": str(next_row["slot_id"]),
                    "slot_index": int(next_row["slot_index"]),
                    "show_at": str(next_row["show_at"]),
                    "release_id": str(next_row["release_id"]),
                    "queue_item_id": str(next_row["queue_item_id"]),
                    "queue_status": str(next_row["queue_status"]),
                    "schedule_id": str(next_row["schedule_id"]),
                    "target_date": str(next_row["target_date"]),
                    "config_version": int(next_row["config_version"]),
                    "offline_schedule_version": int(next_row["offline_schedule_version"] or 0),
                }

            planned_sync = None
            try:
                schedule_values = json.loads(str(device["schedule_times_json"] or "[]"))
                schedule_values = validate_offline_schedule(
                    schedule_values,
                    maximum=resolve_offline_schedule_max_slots(
                        {"offline_schedule_max_slots": device["offline_schedule_max_slots"]}
                    ),
                    minimum_gap_minutes=int(
                        device["minimum_schedule_gap_minutes"] or MINIMUM_SCHEDULE_GAP_MINUTES
                    ),
                )
                sync_epoch = next_sync_epoch(
                    now=now,
                    schedule=schedule_values,
                    timezone_name=str(device["timezone"]),
                    lead_minutes=int(device["prefetch_lead_minutes"] or 0),
                    sync_strategy=str(device["sync_strategy"] or "first_display_lead"),
                    sync_time=device["sync_time"],
                    minimum_gap_minutes=int(
                        device["minimum_schedule_gap_minutes"] or MINIMUM_SCHEDULE_GAP_MINUTES
                    ),
                    maximum_slots=resolve_offline_schedule_max_slots(
                        {"offline_schedule_max_slots": device["offline_schedule_max_slots"]}
                    ),
                )
                planned_at = datetime.fromtimestamp(sync_epoch, timezone.utc).isoformat()
                planned_sync = {
                    "at": planned_at,
                    "epoch": int(sync_epoch),
                    "source": "configured_schedule",
                    "strategy": str(device["sync_strategy"] or "first_display_lead"),
                    "sync_time": device["sync_time"],
                }
            except (TypeError, ValueError, json.JSONDecodeError, ZoneInfoNotFoundError, OverflowError):
                planned_sync = None

            next_wake = None
            if next_display_slot is not None or planned_sync is not None:
                display_at = None
                if next_display_slot is not None:
                    try:
                        display_at = datetime.fromisoformat(str(next_display_slot["show_at"]))
                        if display_at.tzinfo is None:
                            display_at = display_at.replace(tzinfo=timezone.utc)
                    except (TypeError, ValueError, OverflowError):
                        display_at = None
                sync_at = None
                if planned_sync is not None:
                    try:
                        sync_at = datetime.fromisoformat(str(planned_sync["at"]))
                    except (TypeError, ValueError, OverflowError):
                        sync_at = None
                if display_at is not None and (sync_at is None or display_at <= sync_at):
                    assert next_display_slot is not None
                    next_wake = {
                        "at": display_at.astimezone(timezone.utc).isoformat(),
                        "epoch": int(display_at.timestamp()),
                        "source": "prepared_schedule",
                        "kind": "display",
                        "slot_id": next_display_slot["slot_id"],
                    }
                elif planned_sync is not None:
                    next_wake = {**planned_sync, "kind": "network_sync"}

            fallback_recovery = None
            event = connection.execute(
                """
                SELECT event,error_code,details_json,created_at FROM device_events
                WHERE device_id=? ORDER BY created_at DESC,id DESC LIMIT 1
                """,
                (device_id,),
            ).fetchone()
            event_details: dict = {}
            if event is not None:
                try:
                    decoded = json.loads(str(event["details_json"] or "{}"))
                    if isinstance(decoded, dict):
                        event_details = decoded
                except (TypeError, ValueError, json.JSONDecodeError):
                    event_details = {}
            fallback = next(
                (
                    event_details.get(key)
                    for key in ("fallback", "fallback_mode", "fallback_reason")
                    if event_details.get(key) is not None
                ),
                None,
            )
            recovery = next(
                (
                    event_details.get(key)
                    for key in ("recovery", "recovery_mode", "recovery_reason")
                    if event_details.get(key) is not None
                ),
                None,
            )
            if fallback is not None or recovery is not None or device["last_error_code"]:
                fallback_recovery = {
                    "fallback": fallback,
                    "recovery": recovery,
                    "last_error_code": device["last_error_code"],
                    "last_error_message": device["last_error_message"],
                    "source_event": event["event"] if event is not None else None,
                    "source_event_at": event["created_at"] if event is not None else None,
                }

            last_known = {
                "firmware_version": device["firmware_version"],
                "status_at": device["last_status_at"] or device["last_seen_at"],
                "ip": device["last_ip"],
                "wake_reason": device["wake_reason"],
                "wifi_rssi": device["wifi_rssi"],
                "free_heap_bytes": device["free_heap_bytes"],
                "free_psram_bytes": device["free_psram_bytes"],
                "battery_percent": device["battery_percent"],
                "last_error_code": device["last_error_code"],
                "last_error_message": device["last_error_message"],
            }
            return {
                "status": "ok",
                "device_id": str(device["id"]),
                "generated_at": now_iso,
                "desired_config_version": int(device["config_version"]),
                "applied_config_version": int(device["acked_config_version"]),
                "desired_offline_schedule_version": int(device["offline_schedule_version"] or 0),
                "applied_offline_schedule_version": int(
                    device["applied_offline_schedule_version"] or 0
                ),
                "offline_schedule_ack_at": device["offline_schedule_ack_at"],
                "next_display_slot": next_display_slot,
                "next_wake": next_wake,
                "next_network_sync": planned_sync,
                "today": today,
                "today_timeline": today_timeline,
                "last_known": last_known,
                "active_schedule": schedule_identity(active_row),
                "staged_schedule": schedule_identity(staged_row),
                "fallback_recovery": fallback_recovery,
            }

    def update_energy_profile(
        self,
        device_id: str,
        *,
        battery_capacity_mah: float | None,
        standby_current_ma: float | None,
        active_current_ma: float | None,
        refreshes_per_day: float,
        battery_reserve_percent: float,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        details = {
            "battery_capacity_mah": battery_capacity_mah,
            "standby_current_ma": standby_current_ma,
            "active_current_ma": active_current_ma,
            "refreshes_per_day": refreshes_per_day,
            "battery_reserve_percent": battery_reserve_percent,
        }
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE devices SET battery_capacity_mah=?,standby_current_ma=?,
                        active_current_ma=?,refreshes_per_day=?,battery_reserve_percent=?,
                        energy_profile_updated_at=?,updated_at=?
                    WHERE id=?
                    """,
                    (
                        battery_capacity_mah,
                        standby_current_ma,
                        active_current_ma,
                        refreshes_per_day,
                        battery_reserve_percent,
                        now,
                        now,
                        device_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise KeyError(device_id)
                connection.execute(
                    """
                    INSERT INTO device_events(device_id,level,event,message,details_json,created_at)
                    VALUES (?,'info','energy_profile_updated','能源估算參數已更新',?,?)
                    """,
                    (device_id, json.dumps(details, ensure_ascii=False), now),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def list_energy_samples(self, device_id: str, *, days: int = 30, limit: int = 5000):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 3650)))).isoformat()
        bounded_limit = max(1, min(int(limit), 10_000))
        with self.database.session() as connection:
            return connection.execute(
                """
                SELECT * FROM (
                    SELECT id,device_id,battery_voltage,battery_percent,
                           battery_percent_estimated,usb_power,refresh_duration_ms,
                           wake_duration_ms,display_updated,temperature_c,humidity_percent,wifi_rssi,
                           wake_reason,recorded_at
                    FROM device_power_samples
                    WHERE device_id=? AND recorded_at>=?
                    ORDER BY recorded_at DESC,id DESC LIMIT ?
                ) ORDER BY recorded_at,id
                """,
                (device_id, cutoff, bounded_limit),
            ).fetchall()

    def list_events(self, limit: int = 100):
        with self.database.session() as connection:
            return connection.execute(
                """
                SELECT e.*,d.name device_name FROM device_events e
                JOIN devices d ON d.id=e.device_id
                ORDER BY e.created_at DESC,e.id DESC LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
