from __future__ import annotations

from base64 import urlsafe_b64encode
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet, InvalidToken

from inktime.app.core.security import (
    hash_device_secret,
    hash_device_token,
    hash_pairing_code,
    hash_pairing_nonce,
    hash_pairing_scope,
    issue_device_secret,
    register_secret,
)
from inktime.app.db import Database
from inktime.app.domain.photopainter.offline_schedule import (
    LEGACY_MAX_OFFLINE_SLOTS,
    MINIMUM_SCHEDULE_GAP_MINUTES,
    OFFLINE_PREPARE_BOOTSTRAP_AT,
    normalize_sync_strategy,
    offline_schedule_capability_state,
    resolve_offline_schedule_max_slots,
    validate_offline_schedule,
)


PAIRING_TTL = timedelta(minutes=5)
PAIRING_POLL_SECONDS = 3
PAIRING_POLL_WINDOW = timedelta(seconds=30)
PAIRING_CODE_ATTEMPT_LIMIT = 5
PAIRING_CLAIM_ATTEMPT_LIMIT = 5
PAIRING_RATE_LIMIT = 5
PAIRING_RATE_WINDOW = timedelta(minutes=5)
PAIRING_MAX_PENDING = 100
PAIRING_RETENTION = timedelta(days=1)
REPAIR_PERMISSION_TTL = timedelta(minutes=10)
PAIRING_CODE_PATTERN = re.compile(r"^[0-9]{6}$")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class DevicePairingError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        error_code: str = "PAIR-001",
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.retry_after = retry_after


class DevicePairingService:
    """Durable possession-verified enrollment and credential lifecycle.

    A request owns the logical device identity until confirm.  The devices row
    is created/enabled only after the ESP32 has persisted the issued secret and
    authenticated the explicit confirm call.
    """

    def __init__(self, database: Database, pepper: str, master_secret: str) -> None:
        self.database = database
        self.pepper = pepper
        key = urlsafe_b64encode(hashlib.sha256(master_secret.encode("utf-8")).digest())
        self._credential_envelope_cipher = Fernet(key)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    def _code_hash(self, value: str) -> str:
        return hash_pairing_code(value, self.pepper)

    def _pairing_display_code(self, pairing_nonce: str) -> str:
        material = b"pairing-display-code-v1\0" + pairing_nonce.encode("utf-8")
        digest = hmac.new(self.pepper.encode("utf-8"), material, hashlib.sha256).digest()
        value = int.from_bytes(digest[:8], "big") % 1_000_000
        return f"{value:06d}"

    def _nonce_hash(self, value: str) -> str:
        return hash_pairing_nonce(value, self.pepper)

    def _scope_hash(self, scope: str, value: str) -> str:
        return hash_pairing_scope(scope, value, self.pepper)

    @staticmethod
    def _validate_text(value: Any, field: str, maximum: int, *, required: bool = False) -> str:
        if value is None:
            text = ""
        elif isinstance(value, str):
            text = value.strip()
        else:
            raise DevicePairingError(f"{field} 必須是字串", error_code="PAIR-004")
        if required and not text:
            raise DevicePairingError(f"{field} 不可空白", error_code="PAIR-004")
        if len(text) > maximum:
            raise DevicePairingError(f"{field} 過長", error_code="PAIR-004")
        return text

    @staticmethod
    def _validate_device_id(value: Any) -> str:
        device_id = DevicePairingService._validate_text(value, "device_id", 128, required=True)
        if not DEVICE_ID_PATTERN.fullmatch(device_id):
            raise DevicePairingError("device_id 格式不合法", error_code="PAIR-004")
        return device_id

    @staticmethod
    def _capabilities(value: Any) -> str:
        if not isinstance(value, dict):
            raise DevicePairingError("capabilities 必須是 JSON object", error_code="PAIR-004")
        if len(value) > 32:
            raise DevicePairingError("capabilities 欄位過多", error_code="PAIR-004")
        safe: dict[str, str | bool | int] = {}
        for key, raw in value.items():
            name = str(key).strip()
            if not name or len(name) > 64:
                raise DevicePairingError("capabilities 欄位名稱不合法", error_code="PAIR-004")
            if type(raw) not in {str, bool, int}:
                raise DevicePairingError("capabilities 欄位型別不合法", error_code="PAIR-004")
            if isinstance(raw, str) and len(raw) > 128:
                raise DevicePairingError("capabilities 欄位過長", error_code="PAIR-004")
            safe[name] = raw
        return json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _capability_max_slots(capabilities_json: Any) -> int:
        try:
            capabilities = json.loads(str(capabilities_json or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            capabilities = {}
        return resolve_offline_schedule_max_slots(capabilities)

    @staticmethod
    def _envelope_max_slots(envelope: dict[str, Any]) -> int:
        return resolve_offline_schedule_max_slots(
            {"offline_schedule_max_slots": envelope.get("offline_schedule_max_slots")}
        )

    @staticmethod
    def _schedule_values(
        raw: Any,
        fallback: str = "08:00",
        *,
        minimum_gap_minutes: int = MINIMUM_SCHEDULE_GAP_MINUTES,
        maximum: int = LEGACY_MAX_OFFLINE_SLOTS,
    ) -> list[str]:
        if raw is None:
            values = [fallback]
        elif isinstance(raw, str):
            values = [item.strip() for item in raw.split(",") if item.strip()]
        elif isinstance(raw, list):
            if any(not isinstance(item, str) for item in raw):
                raise DevicePairingError("schedule_times 格式不合法", error_code="PAIR-004")
            values = [item.strip() for item in raw if isinstance(item, str) and item.strip()]
        else:
            raise DevicePairingError("schedule_times 格式不合法", error_code="PAIR-004")
        if not values or len(values) > maximum or any(not TIME_PATTERN.fullmatch(item) for item in values):
            raise DevicePairingError("schedule_times 格式不合法", error_code="PAIR-004")
        minutes = [int(item[:2]) * 60 + int(item[3:]) for item in values]
        if any(right <= left for left, right in zip(minutes, minutes[1:], strict=False)):
            raise DevicePairingError("schedule_times 必須嚴格遞增", error_code="PAIR-004")
        try:
            validate_offline_schedule(
                values,
                maximum=maximum,
                minimum_gap_minutes=minimum_gap_minutes,
            )
        except ValueError as exc:
            raise DevicePairingError(str(exc), error_code="PAIR-004") from exc
        return values

    @classmethod
    def _normalize_config(
        cls,
        raw: dict[str, Any] | None,
        *,
        fallback_name: str,
        offline_schedule_max_slots: int = LEGACY_MAX_OFFLINE_SLOTS,
    ) -> dict[str, Any]:
        maximum_slots = resolve_offline_schedule_max_slots(
            {"offline_schedule_max_slots": offline_schedule_max_slots}
        )
        source = dict(raw or {})
        name = cls._validate_text(source.get("name"), "name", 100) or fallback_name
        timezone_name = cls._validate_text(source.get("timezone", "Asia/Taipei"), "timezone", 64)
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise DevicePairingError("timezone 不是有效的 IANA 時區", error_code="PAIR-004") from exc
        schedule = cls._validate_text(source.get("schedule", "08:00"), "schedule", 5, required=True)
        if not TIME_PATTERN.fullmatch(schedule):
            raise DevicePairingError("schedule 格式不合法", error_code="PAIR-004")
        minimum_gap_minutes = source.get(
            "minimum_schedule_gap_minutes", MINIMUM_SCHEDULE_GAP_MINUTES
        )
        if type(minimum_gap_minutes) is not int or not 30 <= minimum_gap_minutes <= 360:
            raise DevicePairingError(
                "minimum_schedule_gap_minutes 必須介於 30 到 360", error_code="PAIR-004"
            )
        try:
            sync_strategy, sync_time = normalize_sync_strategy(
                source.get("sync_strategy", "first_display_lead"),
                source.get("sync_time"),
            )
        except ValueError as exc:
            raise DevicePairingError(str(exc), error_code="PAIR-004") from exc
        schedule_times = cls._schedule_values(
            source.get("schedule_times"),
            schedule,
            minimum_gap_minutes=minimum_gap_minutes,
            maximum=maximum_slots,
        )
        rotation = source.get("rotation", 0)
        if type(rotation) is not int or rotation not in {0, 180}:
            raise DevicePairingError("rotation 只支援 0 或 180", error_code="PAIR-004")
        panel_profile = cls._validate_text(source.get("panel_profile", "safe_4c"), "panel_profile", 128, required=True)
        delivery_mode = cls._validate_text(source.get("delivery_mode", "legacy_online"), "delivery_mode", 64)
        if delivery_mode not in {"legacy_online", "inktime_offline_schedule"}:
            raise DevicePairingError("automatic pairing 不支援 Stock delivery mode", status_code=409, error_code="PAIR-003")
        prefetch = source.get("prefetch_lead_minutes", 5)
        if type(prefetch) is not int or not 0 <= prefetch <= 120:
            raise DevicePairingError("prefetch_lead_minutes 必須介於 0 到 120", error_code="PAIR-004")
        button = cls._validate_text(source.get("button_wake_action", "check_new"), "button_wake_action", 64)
        if button not in {"check_new", "local_next"}:
            raise DevicePairingError("button_wake_action 不合法", error_code="PAIR-004")
        frame_orientation = cls._validate_text(source.get("frame_orientation"), "frame_orientation", 32)
        if frame_orientation and frame_orientation not in {"portrait", "landscape"}:
            raise DevicePairingError("frame_orientation 不合法", error_code="PAIR-004")
        layout_mode = cls._validate_text(source.get("layout_mode"), "layout_mode", 64)
        if layout_mode and layout_mode not in {
            "adaptive_memory", "full", "postcard", "photo_info", "photo_pair", "calendar", "weather_sensor"
        }:
            raise DevicePairingError("layout_mode 不合法", error_code="PAIR-004")
        fit_mode = cls._validate_text(source.get("fit_mode"), "fit_mode", 32)
        if fit_mode and fit_mode not in {"contain", "cover"}:
            raise DevicePairingError("fit_mode 不合法", error_code="PAIR-004")
        if schedule not in schedule_times:
            schedule = schedule_times[0]
        return {
            "name": name,
            "timezone": timezone_name,
            "schedule": schedule,
            "schedule_times": schedule_times,
            "rotation": rotation,
            "panel_profile": panel_profile,
            "delivery_mode": delivery_mode,
            "prefetch_lead_minutes": prefetch,
            "button_wake_action": button,
            "minimum_schedule_gap_minutes": minimum_gap_minutes,
            "sync_strategy": sync_strategy,
            "sync_time": sync_time,
            "stock_endpoint_host": None,
            "frame_orientation": frame_orientation or None,
            "layout_mode": layout_mode or None,
            "fit_mode": fit_mode or None,
            "offline_schedule_max_slots": maximum_slots,
        }

    @staticmethod
    def _is_future(value: Any, now: datetime) -> bool:
        if not value:
            return False
        try:
            return datetime.fromisoformat(str(value)) > now
        except ValueError:
            return False

    def _activity(
        self,
        connection,
        *,
        device_id: str | None,
        source_id: str,
        event: str,
        message: str,
    ) -> None:
        """Write state-only audit data; no code, nonce, or credential material."""
        now = self._iso(self._now())
        connection.execute(
            """
            INSERT OR IGNORE INTO activity_events(
                source,source_id,severity,component,event,message,device_id,details_json,created_at
            ) VALUES ('device_pairing',?,'INFO','device_pairing',?,?,?,'{}',?)
            """,
            (source_id[:128], event[:128], message[:500], device_id, now),
        )
        if device_id is not None:
            connection.execute(
                """
                INSERT INTO device_events(device_id,level,event,message,details_json,created_at)
                VALUES (?, 'info', ?, ?, '{}', ?)
                """,
                (device_id, event[:128], message[:500], now),
            )

    def _rate_limit(self, connection, *, ip_address: str, device_id: str, now: datetime) -> None:
        cutoff = self._iso(now - PAIRING_RATE_WINDOW)
        connection.execute("DELETE FROM device_pairing_rate_limits WHERE attempted_at<?", (cutoff,))
        scopes = (("ip", ip_address[:128]), ("device", device_id[:128]))
        for scope, value in scopes:
            scope_hash = self._scope_hash(scope, value)
            count = connection.execute(
                "SELECT COUNT(*) FROM device_pairing_rate_limits WHERE scope=? AND scope_hash=? AND attempted_at>=?",
                (scope, scope_hash, cutoff),
            ).fetchone()[0]
            if int(count) >= PAIRING_RATE_LIMIT:
                raise DevicePairingError("配對請求過多，請稍後再試", status_code=429, error_code="PAIR-002", retry_after=300)
            connection.execute(
                "INSERT INTO device_pairing_rate_limits(scope,scope_hash,attempted_at) VALUES (?,?,?)",
                (scope, scope_hash, self._iso(now)),
            )

    def _expire_requests(self, connection, now: datetime) -> None:
        now_iso = self._iso(now)
        rows = connection.execute(
            """
            SELECT id,device_id,status FROM device_pairing_requests
            WHERE status IN ('pending','approved','credential_issued')
              AND (expires_at<=? OR (status='credential_issued' AND credential_envelope_expires_at<=?))
            """,
            (now_iso, now_iso),
        ).fetchall()
        for row in rows:
            connection.execute(
                """
                UPDATE device_pairing_requests SET status='expired',
                    credential_envelope_ciphertext=NULL,credential_envelope_expires_at=NULL
                WHERE id=?
                """,
                (row["id"],),
            )
            connection.execute(
                """
                UPDATE devices SET pairing_state=CASE WHEN auth_revoked_at IS NULL THEN 'pairing_expired' ELSE pairing_state END,
                    pairing_code_hash=NULL,pairing_expires_at=NULL,updated_at=?
                WHERE id=? AND pairing_state='pairing_pending'
                """,
                (now_iso, row["device_id"]),
            )
        retention_cutoff = self._iso(now - PAIRING_RETENTION)
        connection.execute(
            """
            DELETE FROM device_pairing_requests
            WHERE status IN ('rejected','expired') AND expires_at<?
            """,
            (retention_cutoff,),
        )

    def _active_request(self, connection, device_id: str):
        return connection.execute(
            """
            SELECT * FROM device_pairing_requests
            WHERE device_id=? AND status IN ('pending','approved','credential_issued')
            ORDER BY requested_at DESC LIMIT 1
            """,
            (device_id,),
        ).fetchone()

    def request_pairing(self, payload: dict[str, Any], *, ip_address: str) -> tuple[int, dict[str, Any]]:
        device_id = self._validate_device_id(payload.get("device_id"))
        pairing_nonce = self._validate_text(payload.get("pairing_nonce"), "pairing_nonce", 256, required=True)
        if len(pairing_nonce) < 16:
            raise DevicePairingError("pairing_nonce 熵不足", error_code="PAIR-004")
        firmware_identity = self._validate_text(payload.get("firmware_identity"), "firmware_identity", 256)
        firmware_version = self._validate_text(payload.get("firmware_version"), "firmware_version", 64)
        panel_profile = self._validate_text(payload.get("panel_profile"), "panel_profile", 128)
        device_name = self._validate_text(payload.get("device_name"), "device_name", 100)
        capabilities_json = self._capabilities(payload.get("capabilities", {}))
        maximum_slots = self._capability_max_slots(capabilities_json)
        now = self._now()
        expires = now + PAIRING_TTL
        nonce_hash = self._nonce_hash(pairing_nonce)
        with self.database.transaction() as connection:
            self._expire_requests(connection, now)
            existing_request = self._active_request(connection, device_id)
            if existing_request is not None:
                if hmac.compare_digest(str(existing_request["pairing_nonce_hash"]), nonce_hash):
                    remaining = max(1, int((datetime.fromisoformat(str(existing_request["expires_at"])) - now).total_seconds()))
                    return 200, {
                        "status": str(existing_request["status"]),
                        "pairing_id": str(existing_request["id"]),
                        "device_id": device_id,
                        "pairing_code": self._pairing_display_code(pairing_nonce),
                        "expires_in_seconds": min(remaining, int(PAIRING_TTL.total_seconds())),
                        "server_epoch": int(now.timestamp()),
                        "poll_after_seconds": PAIRING_POLL_SECONDS,
                        "request_reused": True,
                    }
                raise DevicePairingError("此裝置已有待處理配對請求", status_code=409, error_code="PAIR-003")
            if int(connection.execute(
                "SELECT COUNT(*) FROM device_pairing_requests WHERE status IN ('pending','approved','credential_issued')"
            ).fetchone()[0]) >= PAIRING_MAX_PENDING:
                raise DevicePairingError("待處理配對數量已達上限", status_code=429, error_code="PAIR-002", retry_after=300)
            self._rate_limit(connection, ip_address=ip_address, device_id=device_id, now=now)
            device = connection.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if device is not None:
                mode = str(device["auth_mode"] or "legacy_token")
                state = str(device["pairing_state"] or "paired")
                if str(device["delivery_mode"] or "legacy_online") == "stock_compat" or mode in {"legacy_token", "stock"}:
                    raise DevicePairingError("目前無法建立此配對請求", status_code=409, error_code="PAIR-003")
                if state in {"paired", "revoked", "auth_invalid"}:
                    raise DevicePairingError("目前無法建立此配對請求", status_code=409, error_code="PAIR-003")
                if state == "pairing_pending" and device["auth_revoked_at"] and not self._is_future(device["repair_allowed_until"], now):
                    raise DevicePairingError("目前無法建立此配對請求", status_code=409, error_code="PAIR-003")
            config_source: dict[str, Any] = {}
            if device is not None:
                config_source = {
                    "name": device["name"],
                    "timezone": device["timezone"] or "Asia/Taipei",
                    "schedule": device["schedule"] or "08:00",
                    "rotation": device["rotation"] if device["rotation"] is not None else 0,
                    "panel_profile": device["panel_profile"] or "safe_4c",
                    "delivery_mode": device["delivery_mode"] or "legacy_online",
                    "prefetch_lead_minutes": (
                        device["prefetch_lead_minutes"]
                        if device["prefetch_lead_minutes"] is not None
                        else 5
                    ),
                    "button_wake_action": device["button_wake_action"] or "check_new",
                    "minimum_schedule_gap_minutes": (
                        device["minimum_schedule_gap_minutes"]
                        if device["minimum_schedule_gap_minutes"] is not None
                        else MINIMUM_SCHEDULE_GAP_MINUTES
                    ),
                    "sync_strategy": device["sync_strategy"] or "first_display_lead",
                    "sync_time": device["sync_time"],
                    "frame_orientation": device["frame_orientation"],
                    "layout_mode": device["layout_mode"],
                    "fit_mode": device["fit_mode"],
                }
                schedule_values = None
                for field in ("schedule_times_json", "offline_schedule_json"):
                    try:
                        candidate = json.loads(str(device[field] or "[]"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        candidate = None
                    if isinstance(candidate, list) and candidate:
                        schedule_values = candidate
                        break
                if schedule_values is not None:
                    config_source["schedule_times"] = schedule_values
            config_source.update(
                {
                    "name": device_name or config_source.get("name"),
                    "panel_profile": panel_profile or config_source.get("panel_profile", "safe_4c"),
                }
            )
            config = self._normalize_config(
                config_source,
                fallback_name=device_name or f"待配對裝置 {device_id[-6:]}",
                offline_schedule_max_slots=maximum_slots,
            )
            pairing_id = secrets.token_urlsafe(24)
            pairing_code = self._pairing_display_code(pairing_nonce)
            connection.execute(
                """
                INSERT INTO device_pairing_requests(
                    id,device_id,pairing_nonce_hash,pairing_code_hash,firmware_identity,
                    firmware_version,panel_profile,capabilities_json,config_json,expires_at,requested_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    pairing_id,
                    device_id,
                    nonce_hash,
                    self._code_hash(pairing_code),
                    firmware_identity,
                    firmware_version or None,
                    panel_profile or None,
                    capabilities_json,
                    json.dumps(config, ensure_ascii=False, separators=(",", ":")),
                    self._iso(expires),
                    self._iso(now),
                ),
            )
            if device is not None and str(device["pairing_state"] or "") == "pairing_pending":
                connection.execute(
                    "UPDATE devices SET repair_allowed_until=NULL,pairing_requested_at=?,firmware_identity=?,firmware_version=COALESCE(NULLIF(?,''),firmware_version),updated_at=? WHERE id=?",
                    (self._iso(now), firmware_identity or None, firmware_version, self._iso(now), device_id),
                )
            self._activity(
                connection,
                device_id=device_id if device is not None else None,
                source_id=f"{pairing_id}:requested",
                event="pairing_requested",
                message="裝置已提出短效自動配對請求",
            )
        # The code is returned only to the requesting device.  It is never
        # persisted reversibly, included in audit data, or sent to the admin UI.
        return 201, {
            "status": "pending",
            "pairing_id": pairing_id,
            "device_id": device_id,
            "pairing_code": pairing_code,
            "expires_in_seconds": int(PAIRING_TTL.total_seconds()),
            "server_epoch": int(now.timestamp()),
            "poll_after_seconds": PAIRING_POLL_SECONDS,
        }

    def pending_for_admin(self) -> list[dict[str, Any]]:
        now = self._now()
        with self.database.transaction() as connection:
            self._expire_requests(connection, now)
            rows = connection.execute(
                """
                SELECT p.*,d.name AS device_name,d.enabled,d.firmware_version AS device_firmware_version,
                       d.panel_profile AS device_panel_profile,d.auth_mode,d.pairing_state
                FROM device_pairing_requests p LEFT JOIN devices d ON d.id=p.device_id
                WHERE p.status IN ('pending','approved','credential_issued')
                ORDER BY p.requested_at DESC,p.id DESC LIMIT 100
                """
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                capabilities = json.loads(str(row["capabilities_json"] or "{}"))
                if not isinstance(capabilities, dict):
                    capabilities = {}
            except (TypeError, ValueError, json.JSONDecodeError):
                capabilities = {}
            try:
                config = json.loads(str(row["config_json"] or "{}"))
                if not isinstance(config, dict):
                    config = {}
            except (TypeError, ValueError, json.JSONDecodeError):
                config = {}
            device_id = str(row["device_id"])
            result.append(
                {
                    "pairing_id": str(row["id"]),
                    "device_id": device_id,
                    "device_id_display": f"{device_id[:8]}…{device_id[-6:]}" if len(device_id) > 16 else device_id,
                    "device_name": str(config.get("name") or row["device_name"] or ""),
                    "firmware_version": str(row["firmware_version"] or row["device_firmware_version"] or "未知"),
                    "firmware_identity": str(row["firmware_identity"] or ""),
                    "panel_profile": str(config.get("panel_profile") or row["panel_profile"] or row["device_panel_profile"] or ""),
                    "expires_at": str(row["expires_at"]),
                    "attempts": int(row["attempts"] or 0),
                    "claim_attempts": int(row["claim_attempts"] or 0),
                    "status": str(row["status"]),
                    "pairing_state": str(row["pairing_state"] or "pending_enrollment"),
                    "capabilities": capabilities,
                    "config": config,
                }
            )
        return result

    def approve(
        self,
        pairing_id: str,
        pairing_code: str,
        *,
        administrator_id: str,
        device_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pairing_id = self._validate_text(pairing_id, "pairing_id", 128, required=True)
        pairing_code = self._validate_text(pairing_code, "pairing_code", 6, required=True)
        if not PAIRING_CODE_PATTERN.fullmatch(pairing_code):
            raise DevicePairingError("配對碼格式不合法", error_code="PAIR-005")
        now = self._now()
        deferred_error: DevicePairingError | None = None
        with self.database.transaction() as connection:
            self._expire_requests(connection, now)
            row = connection.execute("SELECT * FROM device_pairing_requests WHERE id=?", (pairing_id,)).fetchone()
            if row is None:
                raise DevicePairingError("配對請求不存在或已失效", status_code=404, error_code="PAIR-003")
            status = str(row["status"])
            if status in {"approved", "credential_issued", "confirmed"}:
                return {"status": status, "pairing_id": pairing_id}
            if status != "pending":
                raise DevicePairingError("配對請求不存在或已失效", status_code=410, error_code="PAIR-007")
            attempts = int(row["attempts"] or 0)
            if attempts >= PAIRING_CODE_ATTEMPT_LIMIT:
                connection.execute("UPDATE device_pairing_requests SET status='rejected' WHERE id=?", (pairing_id,))
                deferred_error = DevicePairingError("配對碼嘗試次數已用盡", status_code=429, error_code="PAIR-006")
            elif not hmac.compare_digest(str(row["pairing_code_hash"]), self._code_hash(pairing_code)):
                attempts += 1
                next_status = "rejected" if attempts >= PAIRING_CODE_ATTEMPT_LIMIT else "pending"
                connection.execute(
                    "UPDATE device_pairing_requests SET attempts=?,status=? WHERE id=?",
                    (attempts, next_status, pairing_id),
                )
                deferred_error = DevicePairingError("配對碼不正確", status_code=403, error_code="PAIR-006")
            else:
                device = connection.execute("SELECT * FROM devices WHERE id=?", (row["device_id"],)).fetchone()
                if device is not None:
                    if str(device["auth_mode"] or "legacy_token") != "automatic" or str(device["delivery_mode"] or "legacy_online") == "stock_compat":
                        raise DevicePairingError("裝置不允許自動配對", status_code=409, error_code="PAIR-003")
                    if str(device["pairing_state"] or "") in {"paired", "revoked", "auth_invalid"}:
                        raise DevicePairingError("裝置不允許自動配對", status_code=409, error_code="PAIR-003")
                try:
                    existing_config = json.loads(str(row["config_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    existing_config = {}
                merged_config = dict(existing_config)
                merged_config.update(device_config or {})
                maximum_slots = self._capability_max_slots(row["capabilities_json"])
                config = self._normalize_config(
                    merged_config,
                    fallback_name=f"待配對裝置 {str(row['device_id'])[-6:]}",
                    offline_schedule_max_slots=maximum_slots,
                )
                connection.execute(
                    "UPDATE device_pairing_requests SET status='approved',approved_at=?,approved_by=?,config_json=? WHERE id=?",
                    (self._iso(now), administrator_id[:128], json.dumps(config, ensure_ascii=False, separators=(",", ":")), pairing_id),
                )
                self._activity(
                    connection,
                    device_id=str(row["device_id"]) if device is not None else None,
                    source_id=f"{pairing_id}:approved",
                    event="pairing_approved",
                    message="管理員已核准裝置配對",
                )
        if deferred_error is not None:
            raise deferred_error
        return {"status": "approved", "pairing_id": pairing_id}

    def _decode_envelope(self, row, now: datetime) -> dict[str, Any]:
        expires_at = str(row["credential_envelope_expires_at"] or "")
        if not expires_at or not self._is_future(expires_at, now):
            raise DevicePairingError("配對 credential 已過期，請重新建立配對", status_code=410, error_code="PAIR-007")
        try:
            value = json.loads(self._credential_envelope_cipher.decrypt(bytes(row["credential_envelope_ciphertext"])).decode("utf-8"))
        except (InvalidToken, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DevicePairingError("配對 credential 暫存資料無效", status_code=409, error_code="PAIR-009") from exc
        if not isinstance(value, dict) or not isinstance(value.get("device_secret"), str):
            raise DevicePairingError("配對 credential 暫存資料無效", status_code=409, error_code="PAIR-009")
        return value

    def _claim_response(self, envelope: dict[str, Any], now: datetime) -> dict[str, Any]:
        secret = str(envelope["device_secret"])
        register_secret(secret)
        return {
            "status": "credential_issued",
            "device_id": str(envelope["device_id"]),
            "credential_version": int(envelope["credential_version"]),
            "device_secret": secret,
            "server_time": self._iso(now),
            "config": dict(envelope.get("config") or {}),
        }

    def claim(self, pairing_id: str, pairing_nonce: str) -> tuple[int, dict[str, Any], int | None]:
        pairing_id = self._validate_text(pairing_id, "pairing_id", 128, required=True)
        pairing_nonce = self._validate_text(pairing_nonce, "pairing_nonce", 256, required=True)
        now = self._now()
        deferred_error: DevicePairingError | None = None
        with self.database.transaction() as connection:
            self._expire_requests(connection, now)
            row = connection.execute("SELECT * FROM device_pairing_requests WHERE id=?", (pairing_id,)).fetchone()
            if row is None:
                raise DevicePairingError("配對請求不存在或已失效", status_code=404, error_code="PAIR-003")
            status = str(row["status"])
            if status == "confirmed":
                raise DevicePairingError("配對已確認且不可重放", status_code=409, error_code="PAIR-009")
            if status in {"expired", "rejected"}:
                raise DevicePairingError("配對請求不存在或已失效", status_code=410, error_code="PAIR-007")
            if not hmac.compare_digest(str(row["pairing_nonce_hash"]), self._nonce_hash(pairing_nonce)):
                attempts = int(row["claim_attempts"] or 0) + 1
                next_status = "rejected" if attempts >= PAIRING_CLAIM_ATTEMPT_LIMIT else status
                if next_status == "rejected":
                    connection.execute(
                        "UPDATE device_pairing_requests SET claim_attempts=?,status=?,credential_envelope_ciphertext=NULL,credential_envelope_expires_at=NULL WHERE id=?",
                        (attempts, next_status, pairing_id),
                    )
                else:
                    connection.execute(
                        "UPDATE device_pairing_requests SET claim_attempts=?,status=? WHERE id=?",
                        (attempts, next_status, pairing_id),
                    )
                deferred_error = DevicePairingError("配對 claim 驗證失敗", status_code=401, error_code="PAIR-008")
            elif status == "pending":
                return 202, {"status": "pairing_pending", "poll_after_seconds": PAIRING_POLL_SECONDS}, PAIRING_POLL_SECONDS
            elif status == "credential_issued":
                return 200, self._claim_response(self._decode_envelope(row, now), now), None
            else:
                device = connection.execute("SELECT * FROM devices WHERE id=?", (row["device_id"],)).fetchone()
                if device is not None:
                    if str(device["auth_mode"] or "legacy_token") != "automatic" or str(device["pairing_state"] or "") in {"paired", "revoked", "auth_invalid"}:
                        raise DevicePairingError("裝置不允許自動配對", status_code=409, error_code="PAIR-003")
                    next_version = max(1, int(device["credential_version"] or 0) + 1)
                else:
                    next_version = 1
                try:
                    config = json.loads(str(row["config_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    config = {}
                maximum_slots = self._capability_max_slots(row["capabilities_json"])
                config = self._normalize_config(
                    config,
                    fallback_name=f"待配對裝置 {str(row['device_id'])[-6:]}",
                    offline_schedule_max_slots=maximum_slots,
                )
                secret = issue_device_secret()
                envelope = {
                    "device_id": str(row["device_id"]),
                    "device_secret": secret,
                    "credential_version": next_version,
                    "config": config,
                    "firmware_version": str(row["firmware_version"] or ""),
                    "firmware_identity": str(row["firmware_identity"] or ""),
                    "offline_schedule_max_slots": maximum_slots,
                }
                envelope_expires = min(datetime.fromisoformat(str(row["expires_at"])), now + PAIRING_TTL)
                encrypted = self._credential_envelope_cipher.encrypt(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                connection.execute(
                    """
                    UPDATE device_pairing_requests SET status='credential_issued',credential_issued_at=?,
                        credential_envelope_ciphertext=?,credential_envelope_expires_at=? WHERE id=?
                    """,
                    (self._iso(now), encrypted, self._iso(envelope_expires), pairing_id),
                )
                if device is not None:
                    connection.execute(
                        "UPDATE devices SET pairing_state='pairing_pending',pairing_code_hash=NULL,pairing_expires_at=?,pairing_claim_attempts=0,updated_at=? WHERE id=?",
                        (self._iso(envelope_expires), self._iso(now), row["device_id"]),
                    )
                self._activity(
                    connection,
                    device_id=str(row["device_id"]) if device is not None else None,
                    source_id=f"{pairing_id}:credential-issued",
                    event="pairing_credential_issued",
                    message="配對 credential 已暫存等待裝置確認",
                )
        if deferred_error is not None:
            raise deferred_error
        return 200, self._claim_response(envelope, now), None

    @staticmethod
    def _secret_matches(stored_hash: Any, secret: str, pepper: str) -> bool:
        if not stored_hash:
            return False
        new_digest = hash_device_secret(secret, pepper)
        legacy_digest = hash_device_token(secret, pepper)
        return hmac.compare_digest(str(stored_hash), new_digest) or hmac.compare_digest(str(stored_hash), legacy_digest)

    def _insert_confirmed_device(self, connection, *, device_id: str, envelope: dict[str, Any], now: datetime) -> None:
        maximum_slots = self._envelope_max_slots(envelope)
        config = self._normalize_config(
            dict(envelope.get("config") or {}),
            fallback_name=f"待配對裝置 {device_id[-6:]}",
            offline_schedule_max_slots=maximum_slots,
        )
        delivery_mode = str(config["delivery_mode"])
        schedule_json = json.dumps(config["schedule_times"], ensure_ascii=False, separators=(",", ":"))
        placeholder = hash_device_token("auto-placeholder-" + secrets.token_urlsafe(32), self.pepper)
        connection.execute(
            """
            INSERT INTO devices(
                id,name,token_hash,enabled,timezone,schedule,rotation,panel_profile,
                frame_orientation,layout_mode,fit_mode,delivery_mode,offline_prefetch_allowed,
                offline_schedule_json,offline_schedule_version,schedule_times_json,prefetch_lead_minutes,
                button_wake_action,minimum_schedule_gap_minutes,sync_strategy,sync_time,
                stock_endpoint_host,auth_mode,pairing_state,credential_version,
                device_secret_hash,paired_at,auth_revoked_at,repair_allowed_until,firmware_version,
                firmware_identity,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                device_id,
                config["name"],
                placeholder,
                1,
                config["timezone"],
                config["schedule"],
                config["rotation"],
                config["panel_profile"],
                config["frame_orientation"],
                config["layout_mode"],
                config["fit_mode"],
                delivery_mode,
                int(delivery_mode == "inktime_offline_schedule"),
                schedule_json,
                int(delivery_mode == "inktime_offline_schedule"),
                schedule_json,
                config["prefetch_lead_minutes"],
                config["button_wake_action"],
                config["minimum_schedule_gap_minutes"],
                config["sync_strategy"],
                config["sync_time"],
                None,
                "automatic",
                "paired",
                int(envelope["credential_version"]),
                hash_device_secret(str(envelope["device_secret"]), self.pepper),
                self._iso(now),
                None,
                None,
                str(envelope.get("firmware_version") or "") or None,
                str(envelope.get("firmware_identity") or "") or None,
                self._iso(now),
                self._iso(now),
            ),
        )
        connection.execute(
            """
            UPDATE devices
            SET offline_schedule_max_slots=?,offline_schedule_capability_state=?,
                next_offline_prepare_at=CASE WHEN ?=1 THEN ? ELSE NULL END
            WHERE id=?
            """,
            (
                maximum_slots,
                offline_schedule_capability_state(maximum_slots),
                int(config["delivery_mode"] == "inktime_offline_schedule"),
                OFFLINE_PREPARE_BOOTSTRAP_AT,
                device_id,
            ),
        )

    def _update_confirmed_device(self, connection, *, device_id: str, envelope: dict[str, Any], now: datetime) -> None:
        device = connection.execute("SELECT auth_mode,delivery_mode FROM devices WHERE id=?", (device_id,)).fetchone()
        if device is None:
            self._insert_confirmed_device(connection, device_id=device_id, envelope=envelope, now=now)
            return
        if str(device["auth_mode"] or "legacy_token") != "automatic" or str(device["delivery_mode"] or "legacy_online") == "stock_compat":
            raise DevicePairingError("Legacy 與 Stock 裝置不可使用自動 credential", status_code=409, error_code="PAIR-003")
        maximum_slots = self._envelope_max_slots(envelope)
        config = self._normalize_config(
            dict(envelope.get("config") or {}),
            fallback_name=f"待配對裝置 {device_id[-6:]}",
            offline_schedule_max_slots=maximum_slots,
        )
        delivery_mode = str(config["delivery_mode"])
        schedule_json = json.dumps(config["schedule_times"], ensure_ascii=False, separators=(",", ":"))
        connection.execute(
            """
            UPDATE devices SET name=?,enabled=1,timezone=?,schedule=?,rotation=?,panel_profile=?,
                frame_orientation=?,layout_mode=?,fit_mode=?,delivery_mode=?,offline_prefetch_allowed=?,
                offline_schedule_json=?,offline_schedule_version=offline_schedule_version+1,
                schedule_times_json=?,prefetch_lead_minutes=?,button_wake_action=?,minimum_schedule_gap_minutes=?,
                sync_strategy=?,sync_time=?,stock_endpoint_host=NULL,
                auth_mode='automatic',pairing_state='paired',credential_version=?,device_secret_hash=?,
                paired_at=?,last_auth_at=NULL,auth_revoked_at=NULL,repair_allowed_until=NULL,
                previous_device_secret_hash=NULL,previous_credential_version=NULL,previous_credential_expires_at=NULL,
                pairing_code_hash=NULL,pairing_expires_at=NULL,pairing_attempts=0,pairing_claim_attempts=0,
                pairing_requested_at=NULL,firmware_version=COALESCE(NULLIF(?,''),firmware_version),
                firmware_identity=COALESCE(NULLIF(?,''),firmware_identity),offline_schedule_max_slots=?,
                offline_schedule_capability_state=?,next_offline_prepare_at=CASE WHEN ?=1 THEN ? ELSE NULL END,
                config_version=config_version+1,updated_at=?
            WHERE id=?
            """,
            (
                config["name"], config["timezone"], config["schedule"], config["rotation"], config["panel_profile"],
                config["frame_orientation"], config["layout_mode"], config["fit_mode"], delivery_mode,
                int(delivery_mode == "inktime_offline_schedule"), schedule_json, schedule_json,
                config["prefetch_lead_minutes"], config["button_wake_action"],
                config["minimum_schedule_gap_minutes"], config["sync_strategy"], config["sync_time"],
                int(envelope["credential_version"]),
                hash_device_secret(str(envelope["device_secret"]), self.pepper), self._iso(now),
                str(envelope.get("firmware_version") or ""), str(envelope.get("firmware_identity") or ""),
                maximum_slots, offline_schedule_capability_state(maximum_slots),
                int(delivery_mode == "inktime_offline_schedule"), OFFLINE_PREPARE_BOOTSTRAP_AT,
                self._iso(now), device_id,
            ),
        )

    def confirm(
        self,
        pairing_id: str,
        device_id: str,
        pairing_nonce: str,
        device_secret: str,
        credential_version: int,
    ) -> tuple[int, dict[str, Any]]:
        pairing_id = self._validate_text(pairing_id, "pairing_id", 128, required=True)
        device_id = self._validate_device_id(device_id)
        pairing_nonce = self._validate_text(pairing_nonce, "pairing_nonce", 256, required=True)
        device_secret = self._validate_text(device_secret, "device_secret", 256, required=True)
        if not device_secret.startswith("ids_") or len(device_secret) < 32:
            raise DevicePairingError("credential 格式不合法", status_code=401, error_code="PAIR-008")
        if type(credential_version) is not int or credential_version < 1:
            raise DevicePairingError("credential_version 不合法", status_code=401, error_code="PAIR-008")
        now = self._now()
        with self.database.transaction() as connection:
            self._expire_requests(connection, now)
            row = connection.execute("SELECT * FROM device_pairing_requests WHERE id=?", (pairing_id,)).fetchone()
            if row is None or str(row["device_id"]) != device_id:
                raise DevicePairingError("配對請求不存在或已失效", status_code=404, error_code="PAIR-003")
            if not hmac.compare_digest(str(row["pairing_nonce_hash"]), self._nonce_hash(pairing_nonce)):
                raise DevicePairingError("confirm pairing 驗證失敗", status_code=401, error_code="PAIR-008")
            status = str(row["status"])
            if status == "confirmed":
                device = connection.execute("SELECT credential_version,device_secret_hash FROM devices WHERE id=?", (device_id,)).fetchone()
                if device is None or int(device["credential_version"] or 0) != credential_version or not self._secret_matches(device["device_secret_hash"], device_secret, self.pepper):
                    raise DevicePairingError("confirm credential 驗證失敗", status_code=401, error_code="PAIR-008")
                register_secret(device_secret)
                return 200, {"status": "already_confirmed", "device_id": device_id, "credential_version": credential_version}
            if status in {"rejected", "expired"}:
                raise DevicePairingError("配對請求已終止，必須重新取得管理員 permission", status_code=410, error_code="PAIR-007")
            if status != "credential_issued":
                if status == "pending":
                    return 202, {"status": "pairing_pending", "poll_after_seconds": PAIRING_POLL_SECONDS}
                raise DevicePairingError("配對尚未產生可確認 credential", status_code=409, error_code="PAIR-009")
            envelope = self._decode_envelope(row, now)
            if str(envelope.get("device_id")) != device_id or int(envelope.get("credential_version", 0)) != credential_version or not hmac.compare_digest(str(envelope.get("device_secret")), device_secret):
                raise DevicePairingError("confirm credential 驗證失敗", status_code=401, error_code="PAIR-008")
            self._update_confirmed_device(connection, device_id=device_id, envelope=envelope, now=now)
            connection.execute(
                """
                UPDATE device_pairing_requests SET status='confirmed',confirmed_at=?,
                    credential_envelope_ciphertext=NULL,credential_envelope_expires_at=NULL WHERE id=?
                """,
                (self._iso(now), pairing_id),
            )
            self._activity(
                connection,
                device_id=device_id,
                source_id=f"{pairing_id}:confirmed",
                event="pairing_confirmed",
                message="裝置已完成 credential confirm",
            )
        register_secret(device_secret)
        return 200, {"status": "confirmed", "device_id": device_id, "credential_version": credential_version}

    def reject(self, pairing_id: str, *, administrator_id: str) -> None:
        pairing_id = self._validate_text(pairing_id, "pairing_id", 128, required=True)
        now = self._now()
        with self.database.transaction() as connection:
            row = connection.execute("SELECT device_id,status FROM device_pairing_requests WHERE id=?", (pairing_id,)).fetchone()
            if row is None:
                raise KeyError(pairing_id)
            if str(row["status"]) not in {"pending", "approved", "credential_issued"}:
                raise DevicePairingError("配對請求不存在或已失效", status_code=410, error_code="PAIR-007")
            connection.execute(
                "UPDATE device_pairing_requests SET status='rejected',credential_envelope_ciphertext=NULL,credential_envelope_expires_at=NULL WHERE id=?",
                (pairing_id,),
            )
            connection.execute(
                "UPDATE devices SET pairing_state=CASE WHEN auth_revoked_at IS NULL THEN 'unpaired' ELSE 'revoked' END,enabled=CASE WHEN auth_revoked_at IS NULL THEN 0 ELSE enabled END,pairing_code_hash=NULL,pairing_expires_at=NULL,repair_allowed_until=NULL,updated_at=? WHERE id=? AND pairing_state='pairing_pending'",
                (self._iso(now), row["device_id"]),
            )
            self._activity(
                connection,
                device_id=str(row["device_id"]) if connection.execute("SELECT 1 FROM devices WHERE id=?", (row["device_id"],)).fetchone() else None,
                source_id=f"{pairing_id}:rejected",
                event="pairing_rejected",
                message=f"管理員 {administrator_id[:128]} 已拒絕裝置配對",
            )

    def revoke(self, device_id: str, *, administrator_id: str) -> None:
        device_id = self._validate_device_id(device_id)
        now = self._now()
        with self.database.transaction() as connection:
            row = connection.execute("SELECT auth_mode FROM devices WHERE id=?", (device_id,)).fetchone()
            if row is None:
                raise KeyError(device_id)
            if str(row["auth_mode"] or "") != "automatic":
                raise DevicePairingError("Legacy 與 Stock 相容模式不使用自動 credential", status_code=409, error_code="PAIR-003")
            connection.execute(
                """
                UPDATE devices SET pairing_state='revoked',auth_revoked_at=?,repair_allowed_until=NULL,
                    previous_device_secret_hash=NULL,previous_credential_version=NULL,previous_credential_expires_at=NULL,updated_at=?
                WHERE id=?
                """,
                (self._iso(now), self._iso(now), device_id),
            )
            connection.execute(
                "UPDATE device_pairing_requests SET status='rejected',credential_envelope_ciphertext=NULL,credential_envelope_expires_at=NULL WHERE device_id=? AND status IN ('pending','approved','credential_issued')",
                (device_id,),
            )
            self._activity(
                connection,
                device_id=device_id,
                source_id=f"{device_id}:{self._iso(now)}:revoked",
                event="credential_revoked",
                message=f"管理員 {administrator_id[:128]} 已撤銷裝置 credential",
            )

    def start_repair(self, device_id: str, *, administrator_id: str) -> None:
        device_id = self._validate_device_id(device_id)
        now = self._now()
        permission_until = now + REPAIR_PERMISSION_TTL
        with self.database.transaction() as connection:
            row = connection.execute("SELECT auth_mode,device_secret_hash,pairing_state FROM devices WHERE id=?", (device_id,)).fetchone()
            if row is None:
                raise KeyError(device_id)
            if str(row["auth_mode"] or "") != "automatic":
                raise DevicePairingError("Legacy 與 Stock 裝置不可直接切換自動配對", status_code=409, error_code="PAIR-003")
            connection.execute(
                """
                UPDATE devices SET pairing_state='pairing_pending',auth_revoked_at=?,repair_allowed_until=?,
                    pairing_code_hash=NULL,pairing_expires_at=NULL,pairing_attempts=0,pairing_claim_attempts=0,
                    pairing_requested_at=NULL,updated_at=? WHERE id=?
                """,
                (self._iso(now), self._iso(permission_until), self._iso(now), device_id),
            )
            connection.execute(
                "UPDATE device_pairing_requests SET status='rejected',credential_envelope_ciphertext=NULL,credential_envelope_expires_at=NULL WHERE device_id=? AND status IN ('pending','approved','credential_issued')",
                (device_id,),
            )
            self._activity(
                connection,
                device_id=device_id,
                source_id=f"{device_id}:{self._iso(now)}:repair",
                event="pairing_repair_enabled",
                message=f"管理員 {administrator_id[:128]} 已允許重新配對",
            )
