from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging

import requests

from inktime.app.core.logging import log_event
from inktime.app.db import Database
from inktime.app.repositories.settings import SecretStore, SettingsRepository


LOGGER = logging.getLogger("notification")
WEBHOOK_SECRET_KEY = "notification.webhook_token"  # noqa: S105 - database key, not a credential


class DeviceNotificationService:
    def __init__(
        self,
        database: Database,
        settings: SettingsRepository,
        secrets: SecretStore,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.secrets = secrets
        self.session = session or requests.Session()

    def token_configured(self) -> bool:
        return bool(self.secrets.get(WEBHOOK_SECRET_KEY))

    def list(self, limit: int = 100):
        with self.database.session() as connection:
            return connection.execute(
                """
                SELECT n.*,d.name device_name FROM device_notifications n
                LEFT JOIN devices d ON d.id=n.device_id
                ORDER BY n.created_at DESC,n.id DESC LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()

    def _webhook_status(self) -> str:
        enabled = bool(self.settings.get("notification.webhook_enabled", False))
        url = str(self.settings.get("notification.webhook_url", "")).strip()
        return "pending" if enabled and url else "disabled"

    def _insert_notification(
        self,
        connection,
        *,
        device_id: str | None,
        kind: str,
        level: str,
        title: str,
        message: str,
        details: dict,
        now: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO device_notifications(
                device_id,kind,level,title,message,details_json,webhook_status,
                webhook_next_attempt_at,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                device_id,
                kind,
                level,
                title,
                message,
                json.dumps(details, ensure_ascii=False),
                self._webhook_status(),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def scan(self, *, now: datetime | None = None) -> dict[str, int]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        current_text = current.isoformat()
        threshold = float(self.settings.get("notification.device_offline_hours", 30))
        cutoff = (current - timedelta(hours=threshold)).isoformat()
        cooldown = float(self.settings.get("notification.device_offline_cooldown_hours", 24))
        repeat_cutoff = (current - timedelta(hours=cooldown)).isoformat()
        repeat_enabled = bool(
            self.settings.get("notification.device_offline_repeat_enabled", False)
        )
        offline_enabled = bool(self.settings.get("notification.device_offline_enabled", True))
        recovery_enabled = bool(self.settings.get("notification.device_recovery_enabled", True))
        counts = {"offline": 0, "offline_reminder": 0, "recovery": 0}

        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                recovered = connection.execute(
                    """
                    SELECT * FROM devices
                    WHERE offline_alert_active=1
                      AND COALESCE(last_status_at,last_seen_at,'') > COALESCE(last_offline_alert_at,'')
                    """
                ).fetchall()
                for device in recovered:
                    connection.execute(
                        """
                        UPDATE devices SET offline_alert_active=0,last_recovery_alert_at=?,updated_at=?
                        WHERE id=? AND offline_alert_active=1
                        """,
                        (current_text, current_text, device["id"]),
                    )
                    message = f"{device['name']} 已重新連線並回報狀態。"
                    connection.execute(
                        """
                        INSERT INTO device_events(device_id,level,event,message,details_json,created_at)
                        VALUES (?,'info','device_recovered',?,?,?)
                        """,
                        (
                            device["id"],
                            message,
                            json.dumps({"last_seen_at": device["last_seen_at"]}, ensure_ascii=False),
                            current_text,
                        ),
                    )
                    if recovery_enabled:
                        self._insert_notification(
                            connection,
                            device_id=str(device["id"]),
                            kind="recovery",
                            level="info",
                            title="InkTime 裝置已恢復",
                            message=message,
                            details={"last_seen_at": device["last_seen_at"]},
                            now=current_text,
                        )
                        counts["recovery"] += 1

                if offline_enabled:
                    offline = connection.execute(
                        """
                        SELECT * FROM devices
                        WHERE enabled=1
                          AND COALESCE(last_status_at,last_seen_at,created_at) < ?
                          AND (
                            offline_alert_active=0
                            OR (?=1 AND COALESCE(last_offline_alert_at,'') < ?)
                          )
                        """,
                        (cutoff, int(repeat_enabled), repeat_cutoff),
                    ).fetchall()
                    for device in offline:
                        reminder = bool(device["offline_alert_active"])
                        kind = "offline_reminder" if reminder else "offline"
                        last_contact = (
                            device["last_status_at"] or device["last_seen_at"] or device["created_at"]
                        )
                        message = (
                            f"{device['name']} 已超過 {threshold:g} 小時未連線；"
                            f"最後活動：{last_contact}。"
                        )
                        connection.execute(
                            """
                            UPDATE devices SET offline_alert_active=1,last_offline_alert_at=?,updated_at=?
                            WHERE id=?
                            """,
                            (current_text, current_text, device["id"]),
                        )
                        connection.execute(
                            """
                            INSERT INTO device_events(
                                device_id,level,event,error_code,message,details_json,created_at
                            ) VALUES (?,'warning',?,'DEVICE-OFFLINE',?,?,?)
                            """,
                            (
                                device["id"],
                                kind,
                                message,
                                json.dumps(
                                    {"last_contact_at": last_contact, "threshold_hours": threshold},
                                    ensure_ascii=False,
                                ),
                                current_text,
                            ),
                        )
                        self._insert_notification(
                            connection,
                            device_id=str(device["id"]),
                            kind=kind,
                            level="warning",
                            title="InkTime 裝置離線" if not reminder else "InkTime 裝置仍離線",
                            message=message,
                            details={"last_contact_at": last_contact, "threshold_hours": threshold},
                            now=current_text,
                        )
                        counts[kind] += 1
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        for kind, count in counts.items():
            if count:
                log_event(
                    LOGGER,
                    logging.WARNING if kind.startswith("offline") else logging.INFO,
                    "裝置離線狀態已變更",
                    event=f"notification_{kind}",
                    details={"count": count, "threshold_hours": threshold},
                )
        return counts

    def create_test(self, *, created_by: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            return self._insert_notification(
                connection,
                device_id=None,
                kind="test",
                level="info",
                title="InkTime Webhook 測試",
                message="這是由 InkTime 管理介面建立的測試通知。",
                details={"created_by": created_by},
                now=now,
            )

    def deliver_pending(self, *, now: datetime | None = None, limit: int = 10) -> dict[str, int]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        current_text = current.isoformat()
        result = {"delivered": 0, "retrying": 0, "failed": 0, "disabled": 0}
        enabled = bool(self.settings.get("notification.webhook_enabled", False))
        url = str(self.settings.get("notification.webhook_url", "")).strip()
        timeout = int(self.settings.get("notification.webhook_timeout_seconds", 10))
        token = self.secrets.get(WEBHOOK_SECRET_KEY) or ""
        with self.database.session() as connection:
            rows = connection.execute(
                """
                SELECT n.*,d.name device_name FROM device_notifications n
                LEFT JOIN devices d ON d.id=n.device_id
                WHERE n.webhook_status IN ('pending','retrying')
                  AND COALESCE(n.webhook_next_attempt_at,n.created_at)<=?
                ORDER BY n.id LIMIT ?
                """,
                (current_text, max(1, min(int(limit), 100))),
            ).fetchall()

        for row in rows:
            if not enabled or not url:
                with self.database.session() as connection:
                    connection.execute(
                        "UPDATE device_notifications SET webhook_status='disabled' WHERE id=?",
                        (row["id"],),
                    )
                result["disabled"] += 1
                continue
            headers = {"Content-Type": "application/json", "User-Agent": "InkTime/2"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            payload = {
                "schema_version": 1,
                "notification_id": int(row["id"]),
                "kind": str(row["kind"]),
                "level": str(row["level"]),
                "title": str(row["title"]),
                "message": str(row["message"]),
                "device": (
                    {"id": str(row["device_id"]), "name": str(row["device_name"])}
                    if row["device_id"]
                    else None
                ),
                "details": json.loads(str(row["details_json"])),
                "created_at": str(row["created_at"]),
            }
            error = ""
            delivered = False
            try:
                response = self.session.post(url, json=payload, headers=headers, timeout=timeout)
                delivered = 200 <= response.status_code < 300
                if not delivered:
                    error = f"HTTP {response.status_code}"
            except requests.RequestException as exc:
                error = f"{type(exc).__name__}: {exc}"[:500]
            attempts = int(row["webhook_attempts"]) + 1
            if delivered:
                status = "delivered"
                next_attempt = None
                result["delivered"] += 1
            elif attempts >= 3:
                status = "failed"
                next_attempt = None
                result["failed"] += 1
            else:
                status = "retrying"
                delay_seconds = (60, 300)[attempts - 1]
                next_attempt = (current + timedelta(seconds=delay_seconds)).isoformat()
                result["retrying"] += 1
            with self.database.session() as connection:
                connection.execute(
                    """
                    UPDATE device_notifications SET webhook_status=?,webhook_attempts=?,
                        webhook_next_attempt_at=?,webhook_delivered_at=?,webhook_last_error=?
                    WHERE id=?
                    """,
                    (
                        status,
                        attempts,
                        next_attempt,
                        current_text if delivered else None,
                        error or None,
                        row["id"],
                    ),
                )
            log_event(
                LOGGER,
                logging.INFO if delivered else logging.WARNING,
                "裝置通知 Webhook 已送達" if delivered else "裝置通知 Webhook 傳送失敗",
                event="notification_webhook_delivered" if delivered else "notification_webhook_failed",
                error_code="" if delivered else "NOTIFY-WEBHOOK",
                details={"notification_id": int(row["id"]), "attempts": attempts, "status": status},
            )
        return result
