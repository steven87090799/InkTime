from __future__ import annotations

from datetime import datetime
import logging
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
        if not settings.get("backup.schedule_enabled", True):
            return
        zone = ZoneInfo(str(settings.get("general.timezone", "Asia/Taipei")))
        now = datetime.now(zone)
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
