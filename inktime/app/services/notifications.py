from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import random
import http.client
import ssl
from uuid import uuid4

import requests

from inktime.app.core.logging import log_event
from inktime.app.core.webhook_safety import PinnedWebhookTransport, UnsafeWebhookURL
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
        session=None,
        max_attempts: int = 5,
        retry_base_seconds: float = 60.0,
        retry_max_seconds: float = 3600.0,
    ) -> None:
        self.database = database
        self.settings = settings
        self.secrets = secrets
        self.session = session or PinnedWebhookTransport()
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_seconds = max(1.0, float(retry_base_seconds))
        self.retry_max_seconds = max(self.retry_base_seconds, float(retry_max_seconds))
        self._metrics = {"attempt": 0, "retry": 0, "success": 0, "failure": 0}

    def observability(self) -> dict[str, int]:
        return dict(self._metrics)

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
        idempotency_source = json.dumps(
            {
                "device_id": device_id,
                "kind": kind,
                "title": title,
                "message": message,
                "details": details,
                "created_at": now,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        idempotency_key = hashlib.sha256(idempotency_source.encode("utf-8")).hexdigest()
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO device_notifications(
                device_id,kind,level,title,message,details_json,webhook_status,
                webhook_next_attempt_at,webhook_idempotency_key,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
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
                idempotency_key,
                now,
            ),
        )
        if cursor.rowcount:
            return int(cursor.lastrowid)
        existing = connection.execute(
            "SELECT id FROM device_notifications WHERE webhook_idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing is None:
            raise RuntimeError("notification idempotency lookup failed")
        return int(existing["id"])

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
        repeat_enabled = bool(self.settings.get("notification.device_offline_repeat_enabled", False))
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
                            f"{device['name']} 已超過 {threshold:g} 小時未連線；最後活動：{last_contact}。"
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
                details={"created_by": created_by, "request_id": str(uuid4())},
                now=now,
            )

    def enqueue_pending(
        self,
        repository,
        job_service,
        *,
        now: datetime | None = None,
        limit: int = 10,
    ) -> dict[str, int]:
        """Claim a bounded due set and hand delivery to the existing Job Queue."""

        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        current_text = current.isoformat()
        claim_until = (current + timedelta(minutes=5)).isoformat()
        bounded = max(1, min(int(limit), 100))
        with self.database.transaction(operation="webhook_claim") as connection:
            rows = connection.execute(
                """
                SELECT id FROM device_notifications
                WHERE webhook_status IN ('pending','retrying')
                  AND COALESCE(webhook_next_attempt_at,created_at)<=?
                  AND (webhook_claimed_until IS NULL OR webhook_claimed_until<=?)
                ORDER BY webhook_next_attempt_at,id LIMIT ?
                """,
                (current_text, current_text, bounded),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"UPDATE device_notifications SET webhook_claimed_until=? WHERE id IN ({placeholders})",  # noqa: S608
                    (claim_until, *ids),
                )
        enqueued = 0
        for notification_id in ids:
            try:
                job_id = repository.create_maintenance(
                    kind="webhook",
                    name="Webhook 通知傳送",
                    settings={"notification_id": notification_id},
                    created_by=None,
                    priority=5,
                    dedupe_key=f"webhook:{notification_id}",
                )
                if str(repository.get(job_id)["status"]) == "pending":
                    job_service.start(job_id)
                enqueued += 1
            except Exception:
                with self.database.transaction(operation="webhook_claim_release") as connection:
                    connection.execute(
                        "UPDATE device_notifications SET webhook_claimed_until=NULL WHERE id=?",
                        (notification_id,),
                    )
                raise
        return {"claimed": len(ids), "enqueued": enqueued}

    def _retry_after_seconds(self, response, current: datetime) -> float | None:
        raw = str(getattr(response, "headers", {}).get("Retry-After", "")).strip()
        if not raw:
            return None
        try:
            return min(self.retry_max_seconds, max(0.0, float(raw)))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(raw)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return min(
                    self.retry_max_seconds,
                    max(0.0, (parsed.astimezone(timezone.utc) - current).total_seconds()),
                )
            except (TypeError, ValueError, OverflowError):
                return None

    def _retry_delay(self, attempts: int) -> float:
        maximum = min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2 ** max(0, attempts - 1)),
        )
        return random.uniform(0.0, maximum)  # noqa: S311 - retry de-synchronization

    def deliver_one(
        self, notification_id: int, *, now: datetime | None = None
    ) -> dict[str, str | int | bool]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        current_text = current.isoformat()
        with self.database.session() as connection:
            row = connection.execute(
                """
                SELECT n.*,d.name device_name FROM device_notifications n
                LEFT JOIN devices d ON d.id=n.device_id WHERE n.id=?
                """,
                (notification_id,),
            ).fetchone()
        if row is None:
            return {"notification_id": notification_id, "status": "missing", "delivered": False}
        if str(row["webhook_status"]) in {"delivered", "failed", "disabled"}:
            return {
                "notification_id": notification_id,
                "status": str(row["webhook_status"]),
                "delivered": str(row["webhook_status"]) == "delivered",
            }
        enabled = bool(self.settings.get("notification.webhook_enabled", False))
        url = str(self.settings.get("notification.webhook_url", "")).strip()
        if not enabled or not url:
            with self.database.transaction(operation="webhook_disable") as connection:
                connection.execute(
                    "UPDATE device_notifications SET webhook_status='disabled',webhook_claimed_until=NULL WHERE id=?",
                    (notification_id,),
                )
            return {"notification_id": notification_id, "status": "disabled", "delivered": False}
        read_timeout = float(self.settings.get("notification.webhook_timeout_seconds", 10))
        connect_timeout = min(3.0, max(1.0, read_timeout))
        token = self.secrets.get(WEBHOOK_SECRET_KEY) or ""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "InkTime/2",
            "Idempotency-Key": str(row["webhook_idempotency_key"]),
        }
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
                {"id": str(row["device_id"]), "name": str(row["device_name"])} if row["device_id"] else None
            ),
            "details": json.loads(str(row["details_json"])),
            "created_at": str(row["created_at"]),
        }
        self._metrics["attempt"] += 1
        response = None
        error = ""
        retryable = True
        delivered = False
        try:
            response = self.session.post(
                url,
                json=payload,
                headers=headers,
                timeout=(connect_timeout, read_timeout),
                allow_redirects=False,
            )
            status_code = int(response.status_code)
            delivered = 200 <= status_code < 300
            retryable = status_code == 429 or status_code >= 500
            if not delivered:
                error = f"HTTP {status_code}"
        except (
            requests.RequestException,
            UnsafeWebhookURL,
            OSError,
            ssl.SSLError,
            http.client.HTTPException,
        ) as exc:
            error = type(exc).__name__
        attempts = int(row["webhook_attempts"]) + 1
        if delivered:
            status = "delivered"
            next_attempt = None
            self._metrics["success"] += 1
        elif not retryable or attempts >= self.max_attempts:
            status = "failed"
            next_attempt = None
            self._metrics["failure"] += 1
        else:
            status = "retrying"
            delay = (
                self._retry_after_seconds(response, current)
                if response is not None and int(response.status_code) == 429
                else None
            )
            delay = self._retry_delay(attempts) if delay is None else delay
            next_attempt = (current + timedelta(seconds=delay)).isoformat()
            self._metrics["retry"] += 1
        with self.database.transaction(operation="webhook_result") as connection:
            connection.execute(
                """
                UPDATE device_notifications SET webhook_status=?,webhook_attempts=?,
                    webhook_next_attempt_at=?,webhook_delivered_at=?,webhook_last_error=?,
                    webhook_claimed_until=NULL
                WHERE id=? AND webhook_status IN ('pending','retrying')
                """,
                (
                    status,
                    attempts,
                    next_attempt,
                    current_text if delivered else None,
                    error or None,
                    notification_id,
                ),
            )
        log_event(
            LOGGER,
            logging.INFO if delivered else logging.WARNING,
            "裝置通知 Webhook 已送達" if delivered else "裝置通知 Webhook 傳送未完成",
            event=(
                "notification_webhook_success"
                if delivered
                else "notification_webhook_retry"
                if status == "retrying"
                else "notification_webhook_failure"
            ),
            error_code="" if delivered else "NOTIFY-WEBHOOK",
            details={"notification_id": notification_id, "attempts": attempts, "status": status},
        )
        return {"notification_id": notification_id, "status": status, "delivered": delivered}

    def deliver_pending(self, *, now: datetime | None = None, limit: int = 10) -> dict[str, int]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        current_text = current.isoformat()
        result = {"delivered": 0, "retrying": 0, "failed": 0, "disabled": 0}
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
            outcome = self.deliver_one(int(row["id"]), now=current)
            status = str(outcome["status"])
            key = "delivered" if status == "delivered" else status
            if key in result:
                result[key] += 1
        return result
