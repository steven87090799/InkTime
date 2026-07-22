from __future__ import annotations

from datetime import datetime
import logging
import os
import signal
import threading
import time

from zoneinfo import ZoneInfo

from inktime.app.core.logging import configure_logging, log_event


LOGGER = logging.getLogger("scheduler")


class SchedulerRunner:
    def __init__(self, app) -> None:
        self.app = app
        self.stop = threading.Event()
        self.last_backup_date: str | None = None
        self.last_notification_scan_at = 0.0

    def request_stop(self, *_args) -> None:
        self.stop.set()

    def tick(self) -> None:
        settings = self.app.extensions["inktime_settings_repository"]
        self.app.extensions["inktime_job_repository"].recover_stale()
        notification_service = self.app.extensions["inktime_notification_service"]
        scan_seconds = int(settings.get("notification.scan_seconds", 300))
        if time.monotonic() - self.last_notification_scan_at >= scan_seconds:
            notification_service.scan()
            self.last_notification_scan_at = time.monotonic()
        # Webhook 的第一、第二次重試分別在 60、300 秒後到期；每次 Scheduler
        # tick 只做一次有索引的 pending 查詢，沒有待送通知時不產生網路或 Log。
        notification_service.deliver_pending()
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
            root_path = str(config.get("root_path") or self.app.config["INKTIME_PHOTO_DIR"])
            mode = str(config.get("mode", "incremental"))
            job_id = repository.create_maintenance(
                kind="scan",
                name=f"排程：{task['name']}",
                priority=4 if mode != "full" else 5,
                dedupe_key=dedupe_key,
                created_by=None,
                settings=common | {
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
                kind="render", name=f"排程：{task['name']}", priority=2, dedupe_key=dedupe_key,
                created_by=None, settings=common | {"photo_ids": [], "display_prepare": config},
            )
        elif task["kind"] == "analysis":
            if config.get("mode") == "disabled" and not force:
                self.app.extensions["inktime_schedule_repository"].mark_enqueued(task, now)
                return
            job_id = self.app.extensions["inktime_job_service"].create_analysis_job(
                name=f"排程：{task['name']}", strategy=str(config.get("strategy", "smart_two_stage")),
                settings=common | {"concurrency": int(config.get("concurrency", 1))}, created_by="system",
                budget_limit=None, priority=3, dedupe_key=dedupe_key,
            )
        else:
            job_id = repository.create_maintenance(
                kind="cleanup", name=f"排程：{task['name']}", priority=6, dedupe_key=dedupe_key,
                created_by=None, settings=common | config,
            )
        if str(repository.get(job_id)["status"]) == "pending":
            self.app.extensions["inktime_job_service"].start(job_id)
        self.app.extensions["inktime_schedule_repository"].mark_enqueued(task, now)
        log_event(LOGGER, logging.INFO, "已建立排程背景工作", event="scheduled_task_enqueued", job_id=job_id,
                  details={"task": task["key"], "priority": repository.get(job_id)["priority"]})

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
    from server import app

    runner = SchedulerRunner(app)
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)
    with app.app_context():
        runner.run_forever()


if __name__ == "__main__":
    main()
