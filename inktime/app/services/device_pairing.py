from __future__ import annotations

from base64 import urlsafe_b64encode
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from inktime.app.core.security import (
    hash_device_secret,
    hash_device_token,
    issue_device_secret,
    register_secret,
)
from inktime.app.db import Database


PAIRING_TTL = timedelta(minutes=5)
PAIRING_POLL_SECONDS = 3
PAIRING_CODE_ATTEMPT_LIMIT = 5
PAIRING_CLAIM_ATTEMPT_LIMIT = 5
PAIRING_RATE_LIMIT = 10
PAIRING_RATE_WINDOW = timedelta(minutes=5)
CREDENTIAL_ROTATION_OVERLAP = timedelta(minutes=10)
PAIRING_CODE_PATTERN = re.compile(r"^[0-9]{6}$")
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
    """Durable pairing and credential lifecycle independent of Flask routes."""

    def __init__(self, database: Database, pepper: str, master_secret: str) -> None:
        self.database = database
        self.pepper = pepper
        key = urlsafe_b64encode(hashlib.sha256(master_secret.encode("utf-8")).digest())
        self._pairing_code_cipher = Fernet(key)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    def _hash(self, value: str) -> str:
        return hash_device_token(value, self.pepper)

    def _rate_limit(self, connection, *, ip_address: str, device_id: str, now: datetime) -> None:
        cutoff = self._iso(now - PAIRING_RATE_WINDOW)
        connection.execute(
            "DELETE FROM device_pairing_rate_limits WHERE attempted_at<?", (cutoff,)
        )
        scopes = (
            ("ip", self._hash(ip_address[:128])),
            ("device", self._hash(device_id[:128])),
        )
        for scope, scope_hash in scopes:
            count = connection.execute(
                "SELECT COUNT(*) FROM device_pairing_rate_limits WHERE scope=? AND scope_hash=? AND attempted_at>=?",
                (scope, scope_hash, cutoff),
            ).fetchone()[0]
            if int(count) >= PAIRING_RATE_LIMIT:
                raise DevicePairingError(
                    "配對請求過多，請稍後再試",
                    status_code=429,
                    error_code="PAIR-002",
                    retry_after=300,
                )
            connection.execute(
                "INSERT INTO device_pairing_rate_limits(scope,scope_hash,attempted_at) VALUES (?,?,?)",
                (scope, scope_hash, self._iso(now)),
            )

    @staticmethod
    def _capabilities(value: Any) -> str:
        if not isinstance(value, dict):
            raise DevicePairingError(
                "capabilities 必須是 JSON object",
                error_code="PAIR-004",
            )
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
    def _validate_device_id(value: Any) -> str:
        if not isinstance(value, str):
            raise DevicePairingError("device_id 必須是字串", error_code="PAIR-004")
        device_id = value.strip()
        if not device_id:
            raise DevicePairingError("device_id 不可空白", error_code="PAIR-004")
        if not DEVICE_ID_PATTERN.fullmatch(device_id):
            raise DevicePairingError("device_id 格式不合法", error_code="PAIR-004")
        return device_id

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

    def _activity(self, connection, *, device_id: str, source_id: str, event: str, message: str) -> None:
        # The activity/audit row intentionally contains only identifiers and
        # state transitions.  Pairing codes and Device Secrets never enter it.
        now = self._iso(self._now())
        connection.execute(
            """
            INSERT OR IGNORE INTO activity_events(
                source,source_id,severity,component,event,message,device_id,details_json,created_at
            ) VALUES ('device_pairing',?,'INFO','device_pairing',?,?,?,'{}',?)
            """,
            (source_id[:128], event[:128], message[:500], device_id, now),
        )
        connection.execute(
            """
            INSERT INTO device_events(device_id,level,event,message,details_json,created_at)
            VALUES (?, 'info', ?, ?, '{}', ?)
            """,
            (device_id, event[:128], message[:500], now),
        )

    def _expire_requests(self, connection, now: datetime) -> None:
        now_iso = self._iso(now)
        rows = connection.execute(
            """
            SELECT id,device_id FROM device_pairing_requests
            WHERE status IN ('pending','approved') AND expires_at<=?
            """,
            (now_iso,),
        ).fetchall()
        if not rows:
            return
        connection.execute(
            """
            UPDATE device_pairing_requests SET status='expired'
            WHERE status IN ('pending','approved') AND expires_at<=?
            """,
            (now_iso,),
        )
        for row in rows:
            connection.execute(
                """
                UPDATE devices SET pairing_state='pairing_expired',updated_at=?
                WHERE id=? AND pairing_state='pairing_pending'
                """,
                (now_iso, row["device_id"]),
            )

    def request_pairing(
        self,
        payload: dict[str, Any],
        *,
        ip_address: str,
    ) -> dict[str, Any]:
        device_id = self._validate_device_id(payload.get("device_id"))
        pairing_nonce = self._validate_text(payload.get("pairing_nonce"), "pairing_nonce", 256, required=True)
        if len(pairing_nonce) < 16:
            raise DevicePairingError("pairing_nonce 熵不足", error_code="PAIR-004")
        firmware_identity = self._validate_text(
            payload.get("firmware_identity"), "firmware_identity", 256
        )
        firmware_version = self._validate_text(payload.get("firmware_version"), "firmware_version", 64)
        panel_profile = self._validate_text(payload.get("panel_profile"), "panel_profile", 128)
        device_name = self._validate_text(payload.get("device_name"), "device_name", 100)
        capabilities_json = self._capabilities(payload.get("capabilities", {}))
        now = self._now()
        expires = now + PAIRING_TTL
        pairing_id = secrets.token_urlsafe(24)
        pairing_code = f"{secrets.randbelow(1_000_000):06d}"
        with self.database.transaction() as connection:
            self._expire_requests(connection, now)
            self._rate_limit(connection, ip_address=ip_address, device_id=device_id, now=now)
            device = connection.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if device is None:
                placeholder = "auto-placeholder-" + secrets.token_urlsafe(32)
                connection.execute(
                    """
                    INSERT INTO devices(
                        id,name,token_hash,enabled,timezone,schedule,rotation,panel_profile,
                        delivery_mode,offline_prefetch_allowed,offline_schedule_json,
                        offline_schedule_version,schedule_times_json,prefetch_lead_minutes,
                        button_wake_action,auth_mode,pairing_state,credential_version,
                        firmware_version,firmware_identity,created_at,updated_at
                    ) VALUES (?,?,?,1,'Asia/Taipei','08:00',0,?,'legacy_online',0,'[]',0,'[\"08:00\"]',5,
                              'check_new','automatic','unpaired',0,?,?,?,?)
                    """,
                    (
                        device_id,
                        device_name or f"待配對裝置 {device_id[-6:]}",
                        self._hash(placeholder),
                        panel_profile or "safe_4c",
                        firmware_version or None,
                        firmware_identity or None,
                        self._iso(now),
                        self._iso(now),
                    ),
                )
                device = connection.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if (
                str(device["delivery_mode"] or "legacy_online") == "stock_compat"
                or str(device["auth_mode"] or "legacy_token") != "automatic"
                or not bool(device["enabled"])
                or str(device["pairing_state"] or "")
                not in {"unpaired", "pairing_pending", "pairing_expired"}
            ):
                # Do not disclose whether an identity belongs to a Legacy,
                # Stock, paired, disabled, or otherwise restricted row.
                raise DevicePairingError("目前無法建立此配對請求", status_code=409, error_code="PAIR-003")
            connection.execute(
                "UPDATE device_pairing_requests SET status='expired' WHERE device_id=? AND status IN ('pending','approved')",
                (device_id,),
            )
            connection.execute(
                """
                INSERT INTO device_pairing_requests(
                    id,device_id,pairing_nonce_hash,pairing_code_hash,pairing_code_ciphertext,
                    firmware_identity,firmware_version,panel_profile,capabilities_json,
                    expires_at,requested_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    pairing_id,
                    device_id,
                    self._hash(pairing_nonce),
                    self._hash(pairing_code),
                    self._pairing_code_cipher.encrypt(pairing_code.encode("ascii")),
                    firmware_identity,
                    firmware_version or None,
                    panel_profile or None,
                    capabilities_json,
                    self._iso(expires),
                    self._iso(now),
                ),
            )
            connection.execute(
                """
                UPDATE devices SET auth_mode='automatic',pairing_state='pairing_pending',
                    pairing_code_hash=?,pairing_expires_at=?,pairing_attempts=0,
                    pairing_claim_attempts=0,pairing_requested_at=?,firmware_identity=?,
                    firmware_version=COALESCE(NULLIF(?,''),firmware_version),
                    panel_profile=COALESCE(NULLIF(?,''),panel_profile),
                    name=COALESCE(NULLIF(?,''),name),updated_at=? WHERE id=?
                """,
                (
                    self._hash(pairing_code),
                    self._iso(expires),
                    self._iso(now),
                    firmware_identity or None,
                    firmware_version,
                    panel_profile,
                    device_name,
                    self._iso(now),
                    device_id,
                ),
            )
            self._activity(
                connection,
                device_id=device_id,
                source_id=f"{pairing_id}:requested",
                event="pairing_requested",
                message="裝置已提出短效自動配對請求",
            )
        register_secret(pairing_code)
        return {
            "pairing_id": pairing_id,
            "device_id": device_id,
            "pairing_code": pairing_code,
            "expires_in_seconds": int(PAIRING_TTL.total_seconds()),
            "poll_after_seconds": PAIRING_POLL_SECONDS,
        }

    def pending_for_admin(self) -> list[dict[str, Any]]:
        now = self._now()
        with self.database.transaction() as connection:
            self._expire_requests(connection, now)
            rows = connection.execute(
                """
                SELECT p.*,d.name,d.enabled,d.firmware_version AS device_firmware_version,
                       d.panel_profile AS device_panel_profile,d.auth_mode,d.pairing_state
                FROM device_pairing_requests p JOIN devices d ON d.id=p.device_id
                WHERE p.status IN ('pending','approved')
                ORDER BY p.requested_at DESC,p.id DESC LIMIT 100
                """
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            device_id = str(row["device_id"])
            try:
                code = self._pairing_code_cipher.decrypt(bytes(row["pairing_code_ciphertext"])).decode("ascii")
            except (InvalidToken, ValueError, TypeError):
                code = "不可解密"
            try:
                capabilities = json.loads(str(row["capabilities_json"] or "{}"))
                if not isinstance(capabilities, dict):
                    capabilities = {}
            except (TypeError, ValueError, json.JSONDecodeError):
                capabilities = {}
            result.append(
                {
                    "pairing_id": str(row["id"]),
                    "device_id": device_id,
                    "device_id_display": (
                        f"{device_id[:8]}…{device_id[-6:]}"
                        if len(device_id) > 16 else device_id
                    ),
                    "device_name": str(row["name"] or ""),
                    "firmware_version": str(row["firmware_version"] or row["device_firmware_version"] or "未知"),
                    "firmware_identity": str(row["firmware_identity"] or ""),
                    "panel_profile": str(row["panel_profile"] or row["device_panel_profile"] or ""),
                    "pairing_code": code,
                    "expires_at": str(row["expires_at"]),
                    "attempts": int(row["attempts"] or 0),
                    "claim_attempts": int(row["claim_attempts"] or 0),
                    "status": str(row["status"]),
                    "pairing_state": str(row["pairing_state"]),
                    "capabilities": capabilities,
                }
            )
        return result

    def approve(self, pairing_id: str, pairing_code: str, *, administrator_id: str) -> dict[str, Any]:
        pairing_id = self._validate_text(pairing_id, "pairing_id", 128, required=True)
        pairing_code = self._validate_text(pairing_code, "pairing_code", 6, required=True)
        if not PAIRING_CODE_PATTERN.fullmatch(pairing_code):
            raise DevicePairingError("配對碼格式不合法", error_code="PAIR-005")
        now = self._now()
        with self.database.transaction() as connection:
            self._expire_requests(connection, now)
            row = connection.execute(
                "SELECT * FROM device_pairing_requests WHERE id=?", (pairing_id,)
            ).fetchone()
            if row is None:
                raise DevicePairingError("配對請求不存在或已失效", status_code=404, error_code="PAIR-003")
            if str(row["status"]) == "approved":
                return {"status": "approved", "pairing_id": pairing_id}
            if str(row["status"]) != "pending":
                raise DevicePairingError("配對請求不存在或已失效", status_code=410, error_code="PAIR-007")
            attempts = int(row["attempts"] or 0)
            if attempts >= PAIRING_CODE_ATTEMPT_LIMIT:
                connection.execute(
                    "UPDATE device_pairing_requests SET status='rejected' WHERE id=?", (pairing_id,)
                )
                raise DevicePairingError("配對碼嘗試次數已用盡", status_code=429, error_code="PAIR-006")
            expected = str(row["pairing_code_hash"])
            if not hmac.compare_digest(expected, self._hash(pairing_code)):
                attempts += 1
                status = "rejected" if attempts >= PAIRING_CODE_ATTEMPT_LIMIT else "pending"
                connection.execute(
                    "UPDATE device_pairing_requests SET attempts=?,status=? WHERE id=?",
                    (attempts, status, pairing_id),
                )
                connection.execute(
                    "UPDATE devices SET pairing_attempts=?,updated_at=? WHERE id=?",
                    (attempts, self._iso(now), row["device_id"]),
                )
                raise DevicePairingError("配對碼不正確", status_code=403, error_code="PAIR-006")
            device = connection.execute(
                "SELECT auth_mode,pairing_state,enabled FROM devices WHERE id=?", (row["device_id"],)
            ).fetchone()
            if device is None or str(device["auth_mode"] or "") != "automatic" or not bool(device["enabled"]):
                raise DevicePairingError("裝置不允許自動配對", status_code=409, error_code="PAIR-003")
            connection.execute(
                "UPDATE device_pairing_requests SET status='approved',approved_at=?,approved_by=? WHERE id=?",
                (self._iso(now), administrator_id, pairing_id),
            )
            self._activity(
                connection,
                device_id=str(row["device_id"]),
                source_id=f"{pairing_id}:approved",
                event="pairing_approved",
                message="管理員已核准裝置配對",
            )
        return {"status": "approved", "pairing_id": pairing_id}

    def claim(self, pairing_id: str, pairing_nonce: str) -> tuple[int, dict[str, Any], int | None]:
        pairing_id = self._validate_text(pairing_id, "pairing_id", 128, required=True)
        pairing_nonce = self._validate_text(pairing_nonce, "pairing_nonce", 256, required=True)
        now = self._now()
        with self.database.transaction() as connection:
            self._expire_requests(connection, now)
            row = connection.execute(
                "SELECT * FROM device_pairing_requests WHERE id=?", (pairing_id,)
            ).fetchone()
            if row is None:
                raise DevicePairingError("配對請求不存在或已失效", status_code=404, error_code="PAIR-003")
            if not hmac.compare_digest(str(row["pairing_nonce_hash"]), self._hash(pairing_nonce)):
                attempts = int(row["claim_attempts"] or 0) + 1
                status = "rejected" if attempts >= PAIRING_CLAIM_ATTEMPT_LIMIT else str(row["status"])
                connection.execute(
                    "UPDATE device_pairing_requests SET claim_attempts=?,status=? WHERE id=?",
                    (attempts, status, pairing_id),
                )
                connection.execute(
                    "UPDATE devices SET pairing_claim_attempts=?,updated_at=? WHERE id=?",
                    (attempts, self._iso(now), row["device_id"]),
                )
                raise DevicePairingError("配對 claim 驗證失敗", status_code=401, error_code="PAIR-008")
            status = str(row["status"])
            if status == "pending":
                return 202, {"status": "pairing_pending", "poll_after_seconds": PAIRING_POLL_SECONDS}, PAIRING_POLL_SECONDS
            if status == "expired":
                raise DevicePairingError("配對碼已過期，請重新建立配對", status_code=410, error_code="PAIR-007")
            if status == "rejected":
                raise DevicePairingError("配對請求已拒絕", status_code=403, error_code="PAIR-007")
            if status == "consumed":
                raise DevicePairingError("配對 credential 已領取且不可重放", status_code=409, error_code="PAIR-009")
            device = connection.execute(
                "SELECT * FROM devices WHERE id=?", (row["device_id"],)
            ).fetchone()
            if device is None or str(device["auth_mode"] or "") != "automatic" or not bool(device["enabled"]):
                raise DevicePairingError("裝置不允許自動配對", status_code=409, error_code="PAIR-003")
            secret = issue_device_secret()
            secret_hash = hash_device_secret(secret, self.pepper)
            next_version = int(device["credential_version"] or 0) + 1
            # An administrator-triggered repair follows an explicit revoke;
            # never resurrect that revoked secret through the optional
            # short-lived rotation overlap.
            previous_hash = None if device["auth_revoked_at"] else device["device_secret_hash"]
            previous_version = device["credential_version"] if previous_hash else None
            previous_expires = self._iso(now + CREDENTIAL_ROTATION_OVERLAP) if previous_hash else None
            connection.execute(
                """
                UPDATE devices SET auth_mode='automatic',pairing_state='paired',
                    device_secret_hash=?,credential_version=?,paired_at=?,last_auth_at=NULL,
                    auth_revoked_at=NULL,previous_device_secret_hash=?,
                    previous_credential_version=?,previous_credential_expires_at=?,
                    pairing_code_hash=NULL,pairing_expires_at=NULL,pairing_attempts=0,
                    pairing_claim_attempts=0,pairing_requested_at=NULL,updated_at=?
                WHERE id=?
                """,
                (
                    secret_hash,
                    next_version,
                    self._iso(now),
                    previous_hash,
                    previous_version,
                    previous_expires,
                    self._iso(now),
                    device["id"],
                ),
            )
            connection.execute(
                "UPDATE device_pairing_requests SET status='consumed',consumed_at=? WHERE id=?",
                (self._iso(now), pairing_id),
            )
            self._activity(
                connection,
                device_id=str(device["id"]),
                source_id=f"{pairing_id}:claimed",
                event="pairing_claimed",
                message="裝置已一次性領取 credential",
            )
            config = {
                "panel_profile": str(device["panel_profile"] or "safe_4c"),
                "delivery_mode": str(device["delivery_mode"] or "legacy_online"),
                "button_wake_action": str(device["button_wake_action"] or "check_new"),
            }
        register_secret(secret)
        return 200, {
            "status": "paired",
            "device_id": str(device["id"]),
            "credential_version": next_version,
            "device_secret": secret,
            "server_time": self._iso(now),
            "config": config,
        }, None

    def reject(self, pairing_id: str, *, administrator_id: str) -> None:
        pairing_id = self._validate_text(pairing_id, "pairing_id", 128, required=True)
        now = self._now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT device_id,status FROM device_pairing_requests WHERE id=?", (pairing_id,)
            ).fetchone()
            if row is None:
                raise KeyError(pairing_id)
            if str(row["status"]) not in {"pending", "approved"}:
                raise DevicePairingError("配對請求不存在或已失效", status_code=410, error_code="PAIR-007")
            connection.execute(
                "UPDATE device_pairing_requests SET status='rejected' WHERE id=?", (pairing_id,)
            )
            connection.execute(
                "UPDATE devices SET pairing_state='unpaired',pairing_code_hash=NULL,pairing_expires_at=NULL,updated_at=? WHERE id=? AND pairing_state='pairing_pending'",
                (self._iso(now), row["device_id"]),
            )
            self._activity(
                connection,
                device_id=str(row["device_id"]),
                source_id=f"{pairing_id}:rejected",
                event="pairing_rejected",
                message=f"管理員 {administrator_id[:128]} 已拒絕裝置配對",
            )

    def revoke(self, device_id: str, *, administrator_id: str) -> None:
        now = self._now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT auth_mode FROM devices WHERE id=?", (device_id,)
            ).fetchone()
            if row is None:
                raise KeyError(device_id)
            if str(row["auth_mode"] or "") != "automatic":
                raise DevicePairingError(
                    "Legacy 與 Stock 相容模式不使用自動 credential",
                    status_code=409,
                    error_code="PAIR-003",
                )
            connection.execute(
                """
                UPDATE devices SET pairing_state='revoked',auth_revoked_at=?,
                    previous_credential_expires_at=NULL,updated_at=? WHERE id=?
                """,
                (self._iso(now), self._iso(now), device_id),
            )
            connection.execute(
                "UPDATE device_pairing_requests SET status='rejected' WHERE device_id=? AND status IN ('pending','approved')",
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
        now = self._now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT auth_mode FROM devices WHERE id=?", (device_id,)
            ).fetchone()
            if row is None:
                raise KeyError(device_id)
            if str(row["auth_mode"] or "") != "automatic":
                raise DevicePairingError("Legacy 與 Stock 裝置不可直接切換自動配對", status_code=409, error_code="PAIR-003")
            connection.execute(
                """
                UPDATE devices SET pairing_state='pairing_pending',pairing_code_hash=NULL,
                    pairing_expires_at=NULL,pairing_attempts=0,pairing_claim_attempts=0,
                    pairing_requested_at=NULL,auth_revoked_at=?,updated_at=? WHERE id=?
                """,
                (self._iso(now), self._iso(now), device_id),
            )
            connection.execute(
                "UPDATE device_pairing_requests SET status='rejected' WHERE device_id=? AND status IN ('pending','approved')",
                (device_id,),
            )
            self._activity(
                connection,
                device_id=device_id,
                source_id=f"{device_id}:{self._iso(now)}:repair",
                event="pairing_repair_enabled",
                message=f"管理員 {administrator_id[:128]} 已允許重新配對",
            )
