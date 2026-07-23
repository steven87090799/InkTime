from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfo

from inktime.app.core.security import redact, redact_text


class ObservabilityService:
    """Low-write checks over the platform's existing state and error tables."""

    _ALERT_INTERVAL = timedelta(minutes=5)
    _PROVIDER_WINDOW = timedelta(minutes=15)
    _BATCH_SIZE = 500

    def __init__(self, database, settings, diagnostics, publisher=None):
        self.database, self.settings, self.diagnostics = database, settings, diagnostics
        self.publisher = publisher

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _parse(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    def record(self, severity, component, event, message, **fields):
        severity = severity.upper()
        if severity == "DEBUG" and not self.settings.get("observability.debug_enabled", False):
            return False
        clean = redact(fields)
        now = self._now().isoformat()
        with self.database.session() as c:
            c.execute(
                "INSERT INTO activity_events(source,source_id,severity,component,event,message,job_id,photo_id,device_id,stage,progress_done,progress_total,error_code,trace_id,details_json,created_at) VALUES ('activity',NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    severity, component[:80], event[:80], redact_text(message)[:1000],
                    clean.get("job_id"), clean.get("photo_id"), clean.get("device_id"), clean.get("stage"),
                    clean.get("progress_done"), clean.get("progress_total"), clean.get("error_code"),
                    clean.get("trace_id"), json.dumps(clean, ensure_ascii=False), now,
                ),
            )
        return True

    def heartbeat(self, component: str) -> None:
        """A single small state update; deliberately not an Activity event."""
        now = self._now().isoformat()
        with self.database.session() as c:
            c.execute(
                "INSERT INTO observability_state(key,value_json,updated_at) VALUES (?, '{}', ?) ON CONFLICT(key) DO UPDATE SET updated_at=excluded.updated_at",
                (f"heartbeat:{component}", now),
            )

    def tick(self):
        now = self._now()
        self._disable_expired_debug(now)
        self._check_jobs(now)
        self._check_platform(now)
        self._check_providers(now)
        self._check_releases(now)
        self._check_schedules(now)
        self._check_devices(now)
        self.cleanup(now)

    def _disable_expired_debug(self, now: datetime) -> None:
        expiry = timedelta(minutes=int(self.settings.get("observability.debug_auto_disable_minutes", 60)))
        disabled = False
        with self.database.session() as c:
            row = c.execute("SELECT updated_at FROM settings WHERE key='observability.debug_enabled'").fetchone()
            changed = self._parse(row[0]) if row else None
            if self.settings.get("observability.debug_enabled", False) and changed and now - changed >= expiry:
                c.execute("UPDATE settings SET value_json='false',updated_at=? WHERE key='observability.debug_enabled'", (now.isoformat(),))
                disabled = True
        if disabled:
            self.record("INFO", "observability", "debug_auto_disabled", "Debug 已依期限自動關閉")

    def _check_jobs(self, now: datetime) -> None:
        cutoff = (now - timedelta(minutes=int(self.settings.get("observability.stuck_job_minutes", 5)))).isoformat()
        with self.database.session() as c:
            stale = c.execute("SELECT id,status,heartbeat_at FROM jobs WHERE status IN ('running','retrying') AND heartbeat_at<?", (cutoff,)).fetchall()
        stale_ids = set()
        for job in stale:
            stale_ids.add(str(job["id"]))
            self.alert("worker", "JOB-HEARTBEAT-STALE", f"工作 {job['id']} 卡在 {job['status']}", job_id=job["id"], subject=str(job["id"]), details={"last_event_at": job["heartbeat_at"], "waiting_seconds": int((now - (self._parse(job["heartbeat_at"]) or now)).total_seconds()), "retryable": True, "recommended_page": "/jobs/" + str(job["id"])})
        with self.database.session() as c:
            unresolved = c.execute("SELECT job_id FROM job_errors WHERE error_code='JOB-HEARTBEAT-STALE' AND resolved_at IS NULL").fetchall()
        for row in unresolved:
            if str(row["job_id"] or "") not in stale_ids:
                self.recover("worker", "JOB-HEARTBEAT-STALE", "工作 heartbeat 已恢復", subject=str(row["job_id"] or ""))

    def _check_platform(self, now: datetime) -> None:
        snapshot = self.diagnostics.snapshot() if self.diagnostics else {}
        if snapshot.get("database", {}).get("integrity") != "ok":
            self.alert("sqlite", "SQLITE-INTEGRITY", "SQLite integrity_check 異常", severity="CRITICAL")
        else:
            self.recover("sqlite", "SQLITE-INTEGRITY", "SQLite integrity_check 已恢復正常")
        disk = float(snapshot.get("disk", {}).get("percent", 0))
        if disk >= 95:
            self.alert("storage", "DISK-CRITICAL", f"資料磁碟使用率 {disk:.1f}%", severity="CRITICAL")
        elif disk >= 85:
            self.alert("storage", "DISK-WARNING", f"資料磁碟使用率 {disk:.1f}%")
            self.recover("storage", "DISK-CRITICAL", "資料磁碟使用率已恢復")
        else:
            self.recover("storage", "DISK-CRITICAL", "資料磁碟使用率已恢復")
            self.recover("storage", "DISK-WARNING", "資料磁碟使用率已恢復")
        if int(snapshot.get("queue_length", 0)) and not int(snapshot.get("worker_count", 0)):
            self.alert("worker", "QUEUE-NO-WORKER", "Queue 有待處理項目，但沒有工作中的 Worker", details={"retryable": True, "recommended_page": "/jobs"})
        else:
            self.recover("worker", "QUEUE-NO-WORKER", "Queue 或 Worker 已恢復")

    def _check_providers(self, now: datetime) -> None:
        cutoff = (now - self._PROVIDER_WINDOW).isoformat()
        with self.database.session() as c:
            rows = c.execute("SELECT provider,status,error_code,completed_at,started_at FROM api_usage WHERE COALESCE(completed_at,started_at)>=? ORDER BY id DESC LIMIT 200", (cutoff,)).fetchall()
        by_provider: dict[str, list] = {}
        for row in rows:
            by_provider.setdefault(str(row["provider"]), []).append(row)
        active: set[tuple[str, str]] = set()
        for provider, events in by_provider.items():
            failures = [row for row in events if str(row["status"]).lower() not in {"ok", "success", "completed"}]
            if not failures:
                continue
            codes = " ".join(str(row["error_code"] or "").lower() for row in failures)
            code = "AI-PROVIDER-TIMEOUT" if "timeout" in codes else "AI-PROVIDER-RATE-LIMIT" if ("429" in codes or "rate" in codes) else "AI-PROVIDER-UNAVAILABLE"
            consecutive = 0
            for row in events:
                if str(row["status"]).lower() in {"ok", "success", "completed"}:
                    break
                consecutive += 1
            if consecutive >= 3:
                active.add((provider, code))
                severity = "CRITICAL" if consecutive >= 8 else "ERROR"
                self.alert("provider", code, f"Provider {provider} 在 15 分鐘內連續失敗 {consecutive} 次", severity=severity, subject=provider, details={"waiting_seconds": int(self._PROVIDER_WINDOW.total_seconds()), "retryable": True, "recommended_page": "/providers"})
        # A pending queue with a recently failing provider is a cooldown symptom, not a second state machine.
        with self.database.session() as c:
            pending = int(c.execute("SELECT COUNT(*) FROM job_items WHERE status='pending' AND available_at<?", ((now - timedelta(minutes=15)).isoformat(),)).fetchone()[0])
        if pending and active:
            self.alert("provider", "AI-PROVIDER-COOLDOWN-STUCK", "工作 Queue 已等待 Provider 冷卻超過 15 分鐘", severity="ERROR", details={"waiting_seconds": 900, "retryable": True, "recommended_page": "/jobs"})
        else:
            self.recover("provider", "AI-PROVIDER-COOLDOWN-STUCK", "Provider 冷卻等待已結束")
        with self.database.session() as c:
            unresolved = c.execute("SELECT fingerprint FROM job_errors WHERE component='provider' AND error_code LIKE 'AI-PROVIDER-%' AND resolved_at IS NULL").fetchall()
        for row in unresolved:
            for provider, code in list(active):
                if hashlib.sha256(f"provider:{code}:{provider}".encode()).hexdigest() == row["fingerprint"]:
                    break
            else:
                # Success after the failing window or a healthy test arriving later is recovery.
                for provider in by_provider:
                    for code in ("AI-PROVIDER-TIMEOUT", "AI-PROVIDER-RATE-LIMIT", "AI-PROVIDER-UNAVAILABLE"):
                        if hashlib.sha256(f"provider:{code}:{provider}".encode()).hexdigest() == row["fingerprint"]:
                            self.recover("provider", code, f"Provider {provider} 已恢復成功請求", subject=provider)

    def _check_releases(self, now: datetime) -> None:
        staged_cutoff = (now - timedelta(minutes=20)).isoformat()
        with self.database.session() as c:
            rows = c.execute("SELECT id,status,created_at,failure_reason,reconciliation_status FROM releases WHERE status IN ('staged','staged_failed') OR reconciliation_status!='ok' ORDER BY created_at DESC LIMIT 100").fetchall()
            known_ids = [str(row[0]) for row in c.execute("SELECT id FROM releases ORDER BY created_at DESC LIMIT 1000").fetchall()]
        active: set[tuple[str, str]] = set()
        for row in rows:
            release_id, status = str(row["id"]), str(row["status"])
            if status == "staged" and str(row["created_at"]) < staged_cutoff:
                active.add((release_id, "RELEASE-STUCK"))
                self.alert("release", "RELEASE-STUCK", f"Release {release_id} staged 超過 20 分鐘", severity="ERROR", subject=release_id, details={"last_event_at": row["created_at"], "retryable": True, "recommended_page": "/errors"})
            if status == "staged_failed" or str(row["reconciliation_status"]) == "payload_missing":
                active.add((release_id, "RELEASE-VALIDATION-FAILED"))
                self.alert("release", "RELEASE-VALIDATION-FAILED", f"Release {release_id} 驗證或 Payload 檢查失敗", severity="ERROR", subject=release_id, details={"retryable": False, "recommended_page": "/errors"})
            if str(row["reconciliation_status"]) not in {"ok", "applied", "skipped"}:
                active.add((release_id, "RELEASE-POINTER-DRIFT"))
                self.alert("release", "RELEASE-POINTER-DRIFT", f"Release {release_id} 的 DB 與 latest pointer 狀態不一致", severity="CRITICAL", subject=release_id, details={"retryable": True, "recommended_page": "/diagnostics"})
            if "rollback" in str(row["failure_reason"] or "").lower():
                active.add((release_id, "RELEASE-ROLLBACK-FAILED"))
                self.alert("release", "RELEASE-ROLLBACK-FAILED", f"Release {release_id} 補償或 rollback 失敗", severity="CRITICAL", subject=release_id, details={"retryable": False, "recommended_page": "/errors"})
        with self.database.session() as c:
            errors = c.execute("SELECT fingerprint FROM job_errors WHERE component='release' AND resolved_at IS NULL").fetchall()
        for row in errors:
            for release_id in known_ids:
                for code in ("RELEASE-STUCK", "RELEASE-VALIDATION-FAILED", "RELEASE-POINTER-DRIFT", "RELEASE-ROLLBACK-FAILED"):
                    if (release_id, code) not in active and hashlib.sha256(f"release:{code}:{release_id}".encode()).hexdigest() == row["fingerprint"]:
                        self.recover("release", code, "Release 已發布、修復或安全回滾", subject=release_id)

    def _check_schedules(self, now: datetime) -> None:
        with self.database.session() as c:
            tasks = c.execute("SELECT key,kind,next_run,last_success,last_failure,error_status,timeout_seconds,enabled FROM scheduled_tasks WHERE enabled=1 AND next_run IS NOT NULL ORDER BY next_run LIMIT 50").fetchall()
        for task in tasks:
            due = self._parse(task["next_run"])
            if not due or now <= due + timedelta(seconds=max(300, int(task["timeout_seconds"]))):
                for code in ("SCHEDULE-PREPARE-OVERDUE", "SCHEDULE-RELEASE-OVERDUE", "SCHEDULE-REPEATED-SKIP"):
                    self.recover("scheduler", code, f"排程 {task['key']} 已回到正常執行區間", subject=str(task["key"]))
                continue
            code = "SCHEDULE-PREPARE-OVERDUE" if str(task["key"]) == "display_prepare" else "SCHEDULE-RELEASE-OVERDUE"
            if task["error_status"] and "合格照片" in str(task["error_status"]):
                code, severity = "SCHEDULE-REPEATED-SKIP", "WARNING"
            else:
                severity = "ERROR"
            self.alert("scheduler", code, f"排程 {task['key']} 超過允許延遲仍未完成", severity=severity, subject=str(task["key"]), details={"last_success_at": task["last_success"], "last_event_at": task["last_failure"] or task["next_run"], "waiting_seconds": int((now - due).total_seconds()), "retryable": True, "recommended_page": "/schedules"})
            for alternate in {"SCHEDULE-PREPARE-OVERDUE", "SCHEDULE-RELEASE-OVERDUE", "SCHEDULE-REPEATED-SKIP"} - {code}:
                self.recover("scheduler", alternate, f"排程 {task['key']} 的先前狀態已恢復", subject=str(task["key"]))

    def _device_due(self, assigned_at: str, schedule: str, timezone_name: str, now: datetime) -> bool:
        assigned = self._parse(assigned_at)
        if not assigned:
            return False
        try:
            zone, hour, minute = ZoneInfo(timezone_name), *map(int, schedule.split(":"))
            local = assigned.astimezone(zone)
            wake = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if wake <= local:
                wake += timedelta(days=1)
            return now >= wake.astimezone(timezone.utc) + timedelta(hours=2)
        except (ValueError, TypeError):
            return now >= assigned + timedelta(hours=30)

    def _check_devices(self, now: datetime) -> None:
        with self.database.session() as c:
            rows = c.execute("SELECT d.id,d.enabled,d.schedule,d.timezone,d.last_download_at,d.last_seen_at,d.last_release_id,d.download_failure_count,d.config_version,d.acked_config_version,r.release_id,r.assigned_at FROM device_render_releases r JOIN devices d ON d.id=r.device_id WHERE d.enabled=1 LIMIT 200").fetchall()
        for row in rows:
            device, release = str(row["id"]), str(row["release_id"])
            if not self._device_due(str(row["assigned_at"]), str(row["schedule"]), str(row["timezone"]), now):
                continue
            assigned = self._parse(row["assigned_at"])
            downloaded = self._parse(row["last_download_at"])
            with self.database.session() as c:
                reports = c.execute("SELECT event,error_code,details_json,created_at FROM device_events WHERE device_id=? AND created_at>=? ORDER BY id DESC LIMIT 20", (device, row["assigned_at"])).fetchall()
            verified = displayed = False
            for report in reports:
                try:
                    details = json.loads(str(report["details_json"] or "{}"))
                except json.JSONDecodeError:
                    details = {}
                same_release = str(details.get("release_id", "")) == release
                verified = verified or (same_release and bool(details.get("payload_sha256_verified")))
                displayed = displayed or (same_release and bool(details.get("display_updated")))
            if not downloaded or (assigned and downloaded < assigned):
                self.alert("device", "DEVICE-DOWNLOAD-OVERDUE", f"裝置 {device} 已指派 Release 但尚未下載", severity="WARNING", subject=f"{device}:{release}", details={"device_id": device, "waiting_seconds": int((now - (assigned or now)).total_seconds()), "retryable": True, "recommended_page": "/devices"})
            else:
                self.recover("device", "DEVICE-DOWNLOAD-OVERDUE", "裝置已下載指派 Release", subject=f"{device}:{release}")
            if downloaded and not verified and now - downloaded >= timedelta(hours=2):
                self.alert("device", "DEVICE-VERIFY-OVERDUE", f"裝置 {device} 已下載 Release 但尚未回報 SHA 驗證", severity="WARNING", subject=f"{device}:{release}", details={"device_id": device, "waiting_seconds": int((now - downloaded).total_seconds()), "retryable": True, "recommended_page": "/devices"})
            elif verified:
                self.recover("device", "DEVICE-VERIFY-OVERDUE", "裝置已回報 Payload SHA 驗證", subject=f"{device}:{release}")
            if downloaded and verified and not displayed and now - downloaded >= timedelta(hours=2):
                self.alert("device", "DEVICE-ACK-OVERDUE", f"裝置 {device} 已驗證 Release 但尚未回報 DISPLAY_COMPLETED", severity="WARNING", subject=f"{device}:{release}", details={"device_id": device, "waiting_seconds": int((now - downloaded).total_seconds()), "retryable": True, "recommended_page": "/devices"})
            elif displayed:
                self.recover("device", "DEVICE-ACK-OVERDUE", "裝置已回報 DISPLAY_COMPLETED", subject=f"{device}:{release}")
            if int(row["download_failure_count"] or 0) >= 3:
                self.alert("device", "DEVICE-DISPLAY-FAILED", f"裝置 {device} 連續下載或顯示失敗", severity="ERROR", subject=device, details={"device_id": device, "retryable": True, "recommended_page": "/devices"})
            elif downloaded:
                self.recover("device", "DEVICE-DISPLAY-FAILED", "裝置下載已恢復", subject=device)
            if int(row["acked_config_version"] or 0) < int(row["config_version"] or 0) and self._device_due(str(row["assigned_at"]), str(row["schedule"]), str(row["timezone"]), now):
                self.alert("device", "DEVICE-CONFIG-ACK-OVERDUE", f"裝置 {device} 尚未確認目前設定", severity="WARNING", subject=device, details={"device_id": device, "retryable": True, "recommended_page": "/devices"})
            else:
                self.recover("device", "DEVICE-CONFIG-ACK-OVERDUE", "裝置設定 ACK 已完成", subject=device)

    def alert(self, component, code, message, *, job_id=None, severity="WARNING", subject="", details=None):
        now = self._now().isoformat()
        fp = hashlib.sha256(f"{component}:{code}:{subject or job_id or ''}".encode()).hexdigest()
        clean_details = redact(details or {})
        recorded = False
        with self.database.session() as c:
            found = c.execute("SELECT id,last_seen_at FROM job_errors WHERE fingerprint=? AND resolved_at IS NULL", (fp,)).fetchone()
            if found:
                seen = self._parse(found["last_seen_at"]) or self._now()
                if self._now() - seen >= self._ALERT_INTERVAL:
                    c.execute("UPDATE job_errors SET occurrences=occurrences+1,last_seen_at=?,message=? WHERE id=?", (now, redact_text(message)[:1000], found["id"]))
                    recorded = True
            else:
                c.execute("INSERT INTO job_errors(job_id,component,error_code,fingerprint,severity,message,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?)", (job_id, component, code, fp, severity.lower(), redact_text(message)[:1000], now, now))
                recorded = True
        if recorded:
            self.record(severity, component, "alert", message, job_id=job_id, error_code=code, **clean_details)

    def recover(self, component, code, message, *, subject=""):
        fp = hashlib.sha256(f"{component}:{code}:{subject}".encode()).hexdigest()
        now = self._now().isoformat()
        with self.database.session() as c:
            row = c.execute("SELECT id FROM job_errors WHERE fingerprint=? AND resolved_at IS NULL", (fp,)).fetchone()
            if row:
                c.execute("UPDATE job_errors SET resolved_at=?,resolution_note=? WHERE id=?", (now, "監控已偵測到恢復", row["id"]))
        if row:
            self.record("INFO", component, "recovered", message, error_code=code)

    def cleanup(self, now: datetime | None = None):
        now = now or self._now()
        days = int(self.settings.get("observability.activity_retention_days", 14))
        max_rows = int(self.settings.get("observability.activity_max_rows", 50000))
        cutoff, debug_cutoff = (now - timedelta(days=days)).isoformat(), (now - timedelta(hours=int(self.settings.get("observability.debug_retention_hours", 24)))).isoformat()
        capacity_blocked = False
        with self.database.session() as c:
            state = c.execute("SELECT updated_at FROM observability_state WHERE key='cleanup'").fetchone()
            last = self._parse(state["updated_at"]) if state else None
            if last and now - last < timedelta(hours=1):
                return
            c.execute("DELETE FROM activity_events WHERE id IN (SELECT id FROM activity_events WHERE severity='DEBUG' AND created_at<? ORDER BY id LIMIT ?)", (debug_cutoff, self._BATCH_SIZE))
            c.execute("DELETE FROM activity_events WHERE id IN (SELECT id FROM activity_events WHERE severity='INFO' AND created_at<? ORDER BY id LIMIT ?)", (cutoff, self._BATCH_SIZE))
            c.execute("DELETE FROM activity_events WHERE id IN (SELECT a.id FROM activity_events a WHERE a.severity IN ('WARNING','ERROR') AND a.created_at<? AND NOT EXISTS (SELECT 1 FROM job_errors e WHERE e.error_code=a.error_code AND e.resolved_at IS NULL) ORDER BY a.id LIMIT ?)", (cutoff, self._BATCH_SIZE))
            c.execute("DELETE FROM job_errors WHERE resolved_at IS NOT NULL AND last_seen_at<?", (cutoff,))
            count = int(c.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0])
            if count > max_rows:
                budget = min(self._BATCH_SIZE, count - max_rows)
                deleted = c.execute("DELETE FROM activity_events WHERE id IN (SELECT id FROM activity_events WHERE severity='DEBUG' ORDER BY id LIMIT ?)", (budget,)).rowcount
                if deleted < budget:
                    c.execute("DELETE FROM activity_events WHERE id IN (SELECT id FROM activity_events WHERE severity='INFO' ORDER BY id LIMIT ?)", (budget - deleted,))
                remaining = int(c.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0])
                capacity_blocked = remaining > max_rows
            c.execute("INSERT INTO observability_state(key,value_json,updated_at) VALUES ('cleanup','{}',?) ON CONFLICT(key) DO UPDATE SET updated_at=excluded.updated_at", (now.isoformat(),))
        if capacity_blocked:
            self.alert("observability", "ACTIVITY-CAPACITY-PROTECTED", "Activity 已達上限，但未解決的重要事件受到保留保護", details={"retryable": False, "recommended_page": "/activity"})
