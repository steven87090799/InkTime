from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hmac
import json
import secrets
from typing import Sequence
from uuid import uuid4

from inktime.app.core.security import hash_device_secret, hash_device_token, issue_device_token
from inktime.app.db import Database
from inktime.app.domain.photopainter.offline_schedule import (
    normalize_delivery_contract,
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
                       offline_schedule_version, last_offline_slot, schedule_times_json,
                       prefetch_lead_minutes, button_wake_action, stock_endpoint_host,
                       frame_orientation, layout_mode, fit_mode,
                       auth_mode, pairing_state, credential_version, paired_at, last_auth_at,
                       auth_revoked_at, repair_allowed_until, pairing_expires_at, pairing_attempts,
                       pairing_claim_attempts, pairing_requested_at, firmware_identity
                FROM devices ORDER BY name
                """
            ).fetchall()

    def get(self, device_id: str):
        with self.database.session() as connection:
            return connection.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()

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
        prefetch_lead_minutes: int = 5,
        button_wake_action: str = "check_new",
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
        schedule_values = validate_offline_schedule(schedule_times or offline_schedule or [schedule], maximum=12)
        if not 0 <= int(prefetch_lead_minutes) <= 120:
            raise ValueError("DEVICE-008 prefetch_lead_minutes 必須介於 0 到 120")
        if button_wake_action not in {"check_new", "local_next"}:
            raise ValueError("DEVICE-008 button_wake_action 不合法")
        with self.database.session() as connection:
            connection.execute(
                """
                INSERT INTO devices(
                    id, name, token_hash, enabled, timezone, schedule, rotation, panel_profile,
                    frame_orientation, layout_mode, fit_mode, delivery_mode,
                    offline_prefetch_allowed, offline_schedule_json, offline_schedule_version,
                    schedule_times_json, prefetch_lead_minutes, button_wake_action,
                    stock_endpoint_host, auth_mode, pairing_state, credential_version,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    json.dumps(schedule_values, ensure_ascii=False),
                    int(prefetch_lead_minutes),
                    button_wake_action,
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
        prefetch_lead_minutes: int = 5,
        button_wake_action: str = "check_new",
        stock_endpoint_host: str | None = None,
        rotation: int,
        panel_profile: str,
        frame_orientation: str | None | object = _UNSET,
        layout_mode: str | None | object = _UNSET,
        fit_mode: str | None | object = _UNSET,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    """
                    SELECT timezone,schedule,rotation,panel_profile,delivery_mode,offline_prefetch_allowed,
                           offline_schedule_json,schedule_times_json,prefetch_lead_minutes,
                           button_wake_action,stock_endpoint_host,frame_orientation,layout_mode,fit_mode,
                           auth_mode,pairing_state
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
                try:
                    existing_schedule = json.loads(str(current["schedule_times_json"] or "[]"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    existing_schedule = []
                if not isinstance(existing_schedule, list) or not existing_schedule:
                    try:
                        existing_schedule = json.loads(str(current["offline_schedule_json"] or "[]"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        existing_schedule = []
                selected_schedule = schedule_times or offline_schedule or existing_schedule or [schedule]
                if schedule_times is None and offline_schedule is None and len(existing_schedule) == 1:
                    selected_schedule = [schedule]
                schedule_values = validate_offline_schedule(selected_schedule, maximum=12)
                if not 0 <= int(prefetch_lead_minutes) <= 120:
                    raise ValueError("DEVICE-008 prefetch_lead_minutes 必須介於 0 到 120")
                if button_wake_action not in {"check_new", "local_next"}:
                    raise ValueError("DEVICE-008 button_wake_action 不合法")
                remote_changed = any(
                    (
                        str(current["timezone"]) != timezone_name,
                        str(current["schedule"]) != schedule_values[0],
                        int(current["rotation"]) != rotation,
                        str(current["panel_profile"]) != panel_profile,
                        str(current["delivery_mode"]) != delivery_mode,
                        bool(current["offline_prefetch_allowed"]) != bool(offline_prefetch_allowed),
                        existing_schedule != schedule_values,
                        int(current["prefetch_lead_minutes"]) != int(prefetch_lead_minutes),
                        str(current["button_wake_action"]) != button_wake_action,
                        (current["stock_endpoint_host"] or None) != (stock_endpoint_host or None),
                    )
                )
                cursor = connection.execute(
                    """
                    UPDATE devices
                    SET name=?,enabled=?,timezone=?,schedule=?,rotation=?,panel_profile=?,
                        delivery_mode=?,offline_prefetch_allowed=?,offline_schedule_json=?,schedule_times_json=?,
                        prefetch_lead_minutes=?,button_wake_action=?,stock_endpoint_host=?,
                        offline_schedule_version=offline_schedule_version+CASE WHEN ? THEN 1 ELSE 0 END,
                        frame_orientation=?,layout_mode=?,fit_mode=?,auth_mode=?,pairing_state=?,
                        config_version=config_version+?,updated_at=?
                    WHERE id=?
                    """,
                    (
                        name.strip(),
                        int(enabled),
                        timezone_name,
                        schedule_values[0],
                        rotation,
                        panel_profile,
                        delivery_mode,
                        int(offline_prefetch_allowed),
                        json.dumps(schedule_values, ensure_ascii=False),
                        json.dumps(schedule_values, ensure_ascii=False),
                        int(prefetch_lead_minutes),
                        button_wake_action,
                        stock_endpoint_host,
                        int(remote_changed),
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
                if delivery_mode == "inktime_offline_schedule":
                    incompatible_modes.append("online_queue")
                    # A changed Enhanced configuration invalidates any
                    # already prepared slot snapshot; the next scheduler
                    # job must prepare the new config version instead.
                    if str(current["delivery_mode"]) == delivery_mode and remote_changed:
                        incompatible_modes.append("offline_schedule")
                else:
                    incompatible_modes.append("offline_schedule")
                if incompatible_modes:
                    placeholders = ",".join("?" for _ in incompatible_modes)
                    # The placeholder count comes only from this fixed local mode list.
                    active_items = connection.execute(
                        f"SELECT id,delivery_mode FROM device_content_queue_items "  # noqa: S608
                        f"WHERE device_id=? AND delivery_mode IN ({placeholders}) "
                        "AND status IN ('PENDING','READY','AVAILABLE','DOWNLOADED','ACKNOWLEDGED') "
                        "ORDER BY id",
                        (device_id, *incompatible_modes),
                    ).fetchall()
                    event_type = (
                        "DELIVERY_MODE_TRANSITION_CANCELLED"
                        if str(current["delivery_mode"]) != delivery_mode
                        else "DELIVERY_CONFIG_CHANGED_CANCELLED"
                    )
                    reason = "delivery_mode_transition" if event_type.endswith("TRANSITION_CANCELLED") else "device_config_changed"
                    for item in active_items:
                        item_id = str(item["id"])
                        connection.execute(
                            "UPDATE device_content_queue_items SET status='CANCELLED',last_error_code='QUEUE-005',updated_at=? WHERE id=?",
                            (now, item_id),
                        )
                        connection.execute(
                            "INSERT OR IGNORE INTO device_content_queue_events(queue_item_id,device_id,event_type,idempotency_key,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                            (
                                item_id,
                                device_id,
                                event_type,
                                f"{event_type}:{device_id}:{item_id}:{now}",
                                json.dumps(
                                    {
                                        "reason": reason,
                                        "old_delivery_mode": str(current["delivery_mode"]),
                                        "new_delivery_mode": delivery_mode,
                                        "queue_item_delivery_mode": str(item["delivery_mode"]),
                                    },
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                                now,
                            ),
                        )
                        connection.execute(
                            "UPDATE rollout_targets SET status='cancelled_mode_transition',last_error_code='QUEUE-005',updated_at=? WHERE queue_item_id=?",
                            (now, item_id),
                        )
                    if active_items:
                        connection.execute(
                            "UPDATE device_content_queues SET queue_version=queue_version+?,updated_at=? WHERE device_id=?",
                            (len(active_items), now, device_id),
                        )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if cursor.rowcount != 1:
            raise KeyError(device_id)

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
                        applied_config_version,
                        applied_config_version,
                        applied_config_version,
                        applied_config_version,
                        applied_config_version,
                        applied_config_version,
                        applied_config_version,
                        now,
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
