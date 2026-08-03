from __future__ import annotations

from datetime import date, datetime, time as clock_time, timedelta, timezone
import json
import logging
import os
import signal
import threading
import time

from zoneinfo import ZoneInfo

from inktime.app.core.logging import configure_logging, log_event
from inktime.app.domain.photopainter.offline_schedule import validate_offline_schedule


LOGGER = logging.getLogger("scheduler")


class SchedulerRunner:
    def __init__(self, app) -> None:
        self.app = app
        self.stop = threading.Event()
        self.last_backup_date: str | None = None
        self.last_notification_scan_at = 0.0
        self.last_batch_poll_at = 0.0

    def request_stop(self, *_args) -> None:
        self.stop.set()

    def tick(self) -> None:
        settings = self.app.extensions["inktime_settings_repository"]
        observability = self.app.extensions["inktime_observability_service"]
        observability.heartbeat("scheduler")
        observability.tick()
        self.app.extensions["inktime_job_repository"].recover_stale()
        batch_poll_seconds = int(settings.get("batch.poll_seconds", 300))
        if time.monotonic() - self.last_batch_poll_at >= max(60, batch_poll_seconds):
            try:
                self.app.extensions["inktime_batch_analysis_service"].poll_due(limit=20)
            except Exception as exc:
                log_event(
                    LOGGER,
                    logging.ERROR,
                    "Batch 遠端輪詢失敗；下次排程會重試",
                    event="analysis_batch_poll_failed",
                    error_code="BATCH-POLL-001",
                    details={"error_type": exc.__class__.__name__},
                )
            self.last_batch_poll_at = time.monotonic()
        notification_service = self.app.extensions["inktime_notification_service"]
        scan_seconds = int(settings.get("notification.scan_seconds", 300))
        if time.monotonic() - self.last_notification_scan_at >= scan_seconds:
            notification_service.scan()
            self.last_notification_scan_at = time.monotonic()
        # Scheduler 只做有界 Claim；實際 HTTP 由既有 Job Queue 處理，慢端點
        # 不會卡住排程掃描與備份。
        notification_service.enqueue_pending(
            self.app.extensions["inktime_job_repository"],
            self.app.extensions["inktime_job_service"],
            limit=10,
        )
        zone = ZoneInfo(str(settings.get("general.timezone", "Asia/Taipei")))
        now = datetime.now(zone)
        schedule_repository = self.app.extensions["inktime_schedule_repository"]
        for task in schedule_repository.due(now):
            try:
                self._enqueue_task(task, now)
            except Exception as exc:  # 一項排程失敗絕不可帶倒 Scheduler。
                schedule_repository.record_failure(task, str(exc), now)
                log_event(
                    LOGGER,
                    logging.ERROR,
                    "排程工作建立失敗；其他排程持續執行",
                    event="scheduled_task_failed",
                    error_code="SCHEDULE-001",
                    details={"task": task["key"]},
                )
        self._prepare_due_offline_devices(datetime.now(timezone.utc))
        if not settings.get("backup.schedule_enabled", True):
            return
        today = now.date().isoformat()
        if now.hour == int(settings.get("backup.hour", 3)) and self.last_backup_date != today:
            path = self.app.extensions["inktime_backup_service"].create()
            removed = self.app.extensions["inktime_backup_service"].enforce_retention(
                int(settings.get("backup.retention", 14))
            )
            self.last_backup_date = today
            log_event(
                LOGGER,
                logging.INFO,
                "排程備份完成",
                event="backup_completed",
                details={"filename": path.name, "removed": removed},
            )

    @staticmethod
    def _offline_prefetch_target_date(
        local_now: datetime,
        schedule: list[str],
        lead_minutes: int,
        server_margin_minutes: int = 0,
    ) -> date | None:
        """Return the latest local day whose first slot is due for prefetch."""

        if type(lead_minutes) is not int or not 0 <= lead_minutes <= 120:
            raise ValueError("DEVICE-008 prefetch_lead_minutes 不合法")
        if type(server_margin_minutes) is not int or not 0 <= server_margin_minutes <= 60:
            raise ValueError("DEVICE-008 server_prefetch_margin_minutes 不合法")
        slots = validate_offline_schedule(schedule, maximum=12)
        zone = local_now.tzinfo
        if zone is None:
            raise ValueError("DEVICE-008 裝置時間必須包含時區")

        def slot_at(target: date, slot: str) -> datetime:
            hour, minute = (int(part) for part in slot.split(":"))
            return datetime.combine(target, clock_time(hour, minute), tzinfo=zone)

        def prefetch_at(target: date) -> datetime:
            hour, minute = (int(part) for part in slots[0].split(":"))
            return datetime.combine(target, clock_time(hour, minute), tzinfo=zone) - timedelta(
                minutes=lead_minutes + server_margin_minutes
            )

        target = local_now.date()
        if local_now < prefetch_at(target):
            return None
        # A day whose every display point has passed is no longer a useful
        # today preparation target.  The caller may still add tomorrow after
        # the configured local preparation hour.
        if not any(slot_at(target, slot) > local_now for slot in slots):
            return None
        # A stalled scheduler may miss more than one midnight.  Keep the
        # catch-up bounded while still selecting the newest day that needs a
        # complete schedule.
        for _ in range(370):
            next_target = target + timedelta(days=1)
            if local_now < prefetch_at(next_target):
                break
            target = next_target
        return target

    def _prepare_due_offline_devices(self, now: datetime) -> None:
        offline_schedules = self.app.extensions.get("inktime_offline_schedule_repository")
        if offline_schedules is None:
            return
        job_repository = self.app.extensions["inktime_job_repository"]
        job_service = self.app.extensions["inktime_job_service"]
        settings = self.app.extensions["inktime_settings_repository"]
        try:
            server_margin = int(settings.get("offline.server_prefetch_margin_minutes", 15))
        except (TypeError, ValueError):
            server_margin = 15
        server_margin = max(0, min(server_margin, 60))
        try:
            future_prepare_hour = int(settings.get("offline.future_schedule_prepare_hour_local", 20))
        except (TypeError, ValueError):
            future_prepare_hour = 20
        future_prepare_hour = max(0, min(future_prepare_hour, 23))
        cursor = offline_schedules.prefetch_cursor()
        devices = offline_schedules.due_prefetch_devices(limit=10, after_device_id=cursor)
        for device in devices:
            try:
                timezone_name = str(device["timezone"] or "UTC")
                local_now = now.astimezone(ZoneInfo(timezone_name))
                schedule = validate_offline_schedule(
                    json.loads(str(device["schedule_times_json"] or "[]")), maximum=12
                )
                target = self._offline_prefetch_target_date(
                    local_now,
                    schedule,
                    int(device["prefetch_lead_minutes"] or 0),
                    server_margin,
                )
                if target is None:
                    targets: list[date] = []
                else:
                    targets = [target]
                if local_now.hour >= future_prepare_hour:
                    tomorrow = local_now.date() + timedelta(days=1)
                    if tomorrow not in targets:
                        targets.append(tomorrow)
                for target_date in targets[:2]:
                    target_iso = target_date.isoformat()
                    if offline_schedules.ready_for_device(
                        device_id=str(device["id"]),
                        target_date=target_iso,
                        config_version=int(device["config_version"]),
                    ) is not None:
                        continue
                    dedupe_key = f"offline-prepare:{device['id']}:{target_iso}:{int(device['config_version'])}"
                    job_id = job_repository.create_maintenance(
                        kind="render",
                        name=f"離線排程準備：{str(device['name'])} {target_iso}",
                        priority=2,
                        dedupe_key=dedupe_key,
                        created_by=None,
                        settings={
                            "offline_prepare": {
                                "device_id": str(device["id"]),
                                "target_date": target_iso,
                                "config_version": int(device["config_version"]),
                            },
                            "trigger_source": "offline-scheduler",
                            "timeout_seconds": 1800,
                            "max_retries": 1,
                        },
                    )
                    current = job_repository.get(job_id)
                    if current is not None and str(current["status"]) == "pending":
                        job_service.start(job_id)
                    log_event(
                        LOGGER,
                        logging.INFO,
                        "已建立離線排程準備工作",
                        event="offline_schedule_prepare_enqueued",
                        job_id=job_id,
                        details={
                            "device_id": str(device["id"]),
                            "target_date": target_iso,
                            "config_version": int(device["config_version"]),
                        },
                    )
            except Exception as exc:
                log_event(
                    LOGGER,
                    logging.ERROR,
                    "離線排程準備建立失敗；其他裝置持續執行",
                    event="offline_schedule_prepare_failed",
                    error_code="OFFLINE-001",
                    details={
                        "device_id": str(device.get("id", "")),
                        "error_type": exc.__class__.__name__,
                    },
                )
            finally:
                offline_schedules.advance_prefetch_cursor(str(device["id"]))

    def _enqueue_task(self, task: dict, now: datetime, *, force: bool = False) -> None:
        config = dict(task["config"])
        if not force and not self._within_window(task, now):
            self.app.extensions["inktime_schedule_repository"].record_failure(
                task, "目前不在允許執行時段，已延後", now
            )
            return
        scheduled_at = datetime.fromisoformat(str(task["next_run"])) if task.get("next_run") else now
        if not force and not config.get("catch_up", True) and (now - scheduled_at).total_seconds() > 300:
            self.app.extensions["inktime_schedule_repository"].mark_enqueued(task, now)
            return
        if not force and config.get("delay_high_load") and self._high_load():
            self.app.extensions["inktime_schedule_repository"].record_failure(
                task, "NAS 目前負載偏高，已延後執行", now
            )
            return
        repository = self.app.extensions["inktime_job_repository"]
        dedupe_key = f"scheduled:{task['key']}"
        common = {
            "scheduled_task": task["key"],
            "timeout_seconds": int(task["timeout_seconds"]),
            "max_retries": int(task["retry_count"]),
            "retry_interval_seconds": int(task["retry_interval_seconds"]),
        }
        if task["kind"] == "scan":
            root_path = str(
                config.get("root_path") or self.app.extensions["inktime_runtime_config"].photo_dir
            )
            mode = str(config.get("mode", "incremental"))
            job_id = repository.create_maintenance(
                kind="scan",
                name=f"排程：{task['name']}",
                priority=4 if mode != "full" else 5,
                dedupe_key=dedupe_key,
                created_by=None,
                settings=common
                | {
                    "root_path": root_path,
                    "library_name": str(config.get("library_name", "主要照片庫")),
                    "mode": mode,
                    "build_thumbnails": bool(config.get("build_thumbnails", True)),
                    "trigger_source": "scheduler",
                    "disk_batch_size": int(config.get("batch_size", 500)),
                    "missing_threshold_percent": float(config.get("missing_safe_percent", 10)),
                },
            )
        elif task["kind"] == "render":
            job_id = repository.create_maintenance(
                kind="render",
                name=f"排程：{task['name']}",
                priority=2,
                dedupe_key=dedupe_key,
                created_by=None,
                settings=common | {"photo_ids": [], "display_prepare": config},
            )
        elif task["kind"] == "analysis":
            if config.get("mode") == "disabled" and not force:
                self.app.extensions["inktime_schedule_repository"].mark_enqueued(task, now)
                return
            job_id = self.app.extensions["inktime_job_service"].create_analysis_job(
                name=f"排程：{task['name']}",
                strategy=str(config.get("strategy", "single")),
                settings=common | {"concurrency": int(config.get("concurrency", 1))},
                created_by="system",
                budget_limit=None,
                priority=3,
                dedupe_key=dedupe_key,
            )
        else:
            job_id = repository.create_maintenance(
                kind="cleanup",
                name=f"排程：{task['name']}",
                priority=6,
                dedupe_key=dedupe_key,
                created_by=None,
                settings=common | config,
            )
        if str(repository.get(job_id)["status"]) == "pending":
            self.app.extensions["inktime_job_service"].start(job_id)
        self.app.extensions["inktime_schedule_repository"].mark_enqueued(task, now)
        log_event(
            LOGGER,
            logging.INFO,
            "已建立排程背景工作",
            event="scheduled_task_enqueued",
            job_id=job_id,
            details={"task": task["key"], "priority": repository.get(job_id)["priority"]},
        )

    @staticmethod
    def _high_load() -> bool:
        try:
            cpu_count = os.cpu_count() or 1
            return os.getloadavg()[0] / cpu_count >= 0.85
        except (AttributeError, OSError):
            return False

    @staticmethod
    def _within_window(task: dict, now: datetime) -> bool:
        start = task.get("window_start")
        end = task.get("window_end")
        if not start or not end:
            return True
        current = now.strftime("%H:%M")
        return start <= current <= end if start <= end else current >= start or current <= end

    def run_forever(self) -> None:
        settings = self.app.extensions["inktime_settings_repository"]
        configure_logging(settings_repository=settings)
        log_event(LOGGER, logging.INFO, "排程器已啟動", event="scheduler_started")
        while not self.stop.is_set():
            self.tick()
            configure_logging(settings_repository=settings)
            poll_seconds = int(settings.get("scheduler.poll_seconds", 60))
            self.stop.wait(max(30, min(poll_seconds, 3600)))
        log_event(LOGGER, logging.INFO, "排程器已停止", event="scheduler_stopped")


def main() -> None:
    from inktime.app.bootstrap import bootstrap_services
    from inktime.app.core.runtime_config import resolve_runtime_config

    container = bootstrap_services(resolve_runtime_config(), role="scheduler")
    runner = SchedulerRunner(container)
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)
    try:
        runner.run_forever()
    finally:
        container.close()


if __name__ == "__main__":
    main()
