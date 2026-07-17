from __future__ import annotations

from datetime import datetime
import logging
import signal
import threading

from zoneinfo import ZoneInfo

from inktime.app.core.logging import log_event


LOGGER = logging.getLogger("scheduler")


class SchedulerRunner:
    def __init__(self, app) -> None:
        self.app = app
        self.stop = threading.Event()
        self.last_backup_date: str | None = None

    def request_stop(self, *_args) -> None:
        self.stop.set()

    def tick(self) -> None:
        settings = self.app.extensions["inktime_settings_repository"]
        self.app.extensions["inktime_job_repository"].recover_stale()
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
        while not self.stop.is_set():
            self.tick()
            self.stop.wait(60)


def main() -> None:
    from server import app

    runner = SchedulerRunner(app)
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)
    with app.app_context():
        runner.run_forever()


if __name__ == "__main__":
    main()
