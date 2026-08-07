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
from inktime.app.domain.jobs.failure_policy import (
    FailureClass,
    classify_failure,
    failure_code,
)
from inktime.app.domain.photopainter.offline_schedule import (
    MINIMUM_SCHEDULE_GAP_MINUTES,
    normalize_sync_strategy,
    validate_offline_schedule,
)


LOGGER = logging.getLogger("scheduler")


class SchedulerRunner:
    def __init__(self, app) -> None:
        self.app = app
        self.stop = threading.Event()
        self.last_notification_scan_at = 0.0
        self.last_notification_enqueue_at = 0.0
        self.last_batch_poll_at = 0.0
        self.last_backup_date: str | None = None

    def _record_schedule_exception(self, task: dict, exc: Exception, now: datetime) -> None:
        schedules = self.app.extensions["inktime_schedule_repository"]
        classification = classify_failure(exc)
        message = str(exc)[:1000]
        if classification in {FailureClass.TERMINAL_NO_RETRY, FailureClass.STALE_RECOVERY}:
            schedules.record_terminal(task, message, now)
        else:
            schedules.record_failure(task, message, now)

    def _safe_step(self, name: str, action) -> bool:
        try:
            action()
            return True
        except Exception as exc:
            log_event(
                LOGGER,
                logging.ERROR,
                "Scheduler 子步驟失敗；其他步驟持續執行",
                event="scheduler_step_failed",
                error_code="SCHEDULE-002",
                details={"step": name, "error_type": exc.__class__.__name__},
            )
            return False

    def _enqueue_backup(self, now: datetime, settings) -> None:
        repository = self.app.extensions["inktime_job_repository"]
        service = self.app.extensions["inktime_job_service"]
        today = now.date().isoformat()
        job_id = repository.create_maintenance(
            kind="backup",
            name=f"排程備份：{today}",
            priority=6,
            dedupe_key=f"scheduled-backup:{today}",
            created_by=None,
            settings={
                "scheduled_backup": True,
                "retention_days": int(settings.get("backup.retention", 14)),
                "trigger_source": "scheduler",
            },
        )
        job = repository.get(job_id)
        if job is not None and str(job["status"]) == "pending":
            service.start(job_id)
        log_event(
            LOGGER,
            logging.INFO,
            "已建立排程備份工作",
            event="backup_enqueued",
            job_id=job_id,
            details={"date": today},
        )

    def request_stop(self, *_args) -> None:
        self.stop.set()

    def tick(self) -> None:
        settings = self.app.extensions["inktime_settings_repository"]
        observability = self.app.extensions["inktime_observability_service"]
        self._safe_step(
            "observability_heartbeat",
            lambda: observability.heartbeat("scheduler"),
        )
        self._safe_step("observability_tick", observability.tick)
        self._safe_step(
            "recover_stale",
            self.app.extensions["inktime_job_repository"].recover_stale,
        )
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
            self._safe_step("notification_scan", notification_service.scan)
            self.last_notification_scan_at = time.monotonic()
        # Scheduler 只做有界 Claim；實際 HTTP 由既有 Job Queue 處理，慢端點
        # 不會卡住排程掃描與備份。
        if time.monotonic() - self.last_notification_enqueue_at >= scan_seconds:
            self._safe_step(
                "notification_enqueue",
                lambda: notification_service.enqueue_pending(
                    self.app.extensions["inktime_job_repository"],
                    self.app.extensions["inktime_job_service"],
                    limit=10,
                ),
            )
            self.last_notification_enqueue_at = time.monotonic()
        zone = ZoneInfo(str(settings.get("general.timezone", "Asia/Taipei")))
        now = datetime.now(zone)
        schedule_repository = self.app.extensions["inktime_schedule_repository"]
        job_repository = self.app.extensions["inktime_job_repository"]
        for task in schedule_repository.due(now):
            if job_repository.has_active_dedupe(f"scheduled:{task['key']}"):
                # The scheduled identity is already owned by a live Job.  Move
                # the cron cursor once and let that Job finish; do not create a
                # second queue entry on every scheduler tick.
                schedule_repository.mark_enqueued(task, now)
                log_event(
                    LOGGER,
                    logging.DEBUG,
                    "排程工作已在執行中；略過重複建立",
                    event="scheduled_task_active_skipped",
                    details={"task": task["key"]},
                )
                continue
            try:
                self._enqueue_task(task, now)
            except Exception as exc:  # 一項排程失敗絕不可帶倒 Scheduler。
                self._record_schedule_exception(task, exc, now)
                log_event(
                    LOGGER,
                    logging.ERROR,
                    "排程工作建立失敗；其他排程持續執行",
                    event="scheduled_task_failed",
                    error_code=failure_code(exc),
                    details={"task": task["key"], "failure_class": classify_failure(exc).value},
                )
        self._safe_step(
            "offline_prefetch",
            lambda: self._prepare_due_offline_devices(datetime.now(timezone.utc)),
        )
        if not settings.get("backup.schedule_enabled", True):
            return
        if now.hour == int(settings.get("backup.hour", 3)) and self.last_backup_date != now.date().isoformat():
            if self._safe_step("backup_enqueue", lambda: self._enqueue_backup(now, settings)):
                self.last_backup_date = now.date().isoformat()

    @staticmethod
    def _offline_prefetch_target_date(
        local_now: datetime,
        schedule: list[str],
        lead_minutes: int,
        server_margin_minutes: int = 0,
        sync_strategy: str = "first_display_lead",
        sync_time: str | None = None,
        minimum_gap_minutes: int = MINIMUM_SCHEDULE_GAP_MINUTES,
    ) -> date | None:
        """Return the latest local day whose first slot is due for prefetch."""

        if type(lead_minutes) is not int or not 0 <= lead_minutes <= 120:
            raise ValueError("DEVICE-008 prefetch_lead_minutes 不合法")
        if type(server_margin_minutes) is not int or not 0 <= server_margin_minutes <= 60:
            raise ValueError("DEVICE-008 server_prefetch_margin_minutes 不合法")
        strategy, normalized_sync_time = normalize_sync_strategy(sync_strategy, sync_time)
        slots = validate_offline_schedule(
            schedule, maximum=24, minimum_gap_minutes=minimum_gap_minutes
        )
        zone = local_now.tzinfo
        if zone is None:
            raise ValueError("DEVICE-008 裝置時間必須包含時區")

        def slot_at(target: date, slot: str) -> datetime:
            hour, minute = (int(part) for part in slot.split(":"))
            return datetime.combine(target, clock_time(hour, minute), tzinfo=zone)

        def prefetch_at(target: date) -> datetime:
            if strategy == "fixed_daily":
                assert normalized_sync_time is not None
                hour, minute = (int(part) for part in normalized_sync_time.split(":"))
                return datetime.combine(target, clock_time(hour, minute), tzinfo=zone)
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
        # This helper is intentionally today-only.  A separate tomorrow
        # decision below prevents a due tomorrow preparation point from
        # replacing a still-serviceable later slot today.
        return target

    @staticmethod
    def _offline_tomorrow_prefetch_due(
        local_now: datetime,
        schedule: list[str],
        lead_minutes: int,
        server_margin_minutes: int,
        future_prepare_hour: int,
        sync_strategy: str = "first_display_lead",
        sync_time: str | None = None,
        minimum_gap_minutes: int = MINIMUM_SCHEDULE_GAP_MINUTES,
    ) -> bool:
        """Return whether tomorrow needs technical preparation now."""

        if type(future_prepare_hour) is not int or not 0 <= future_prepare_hour <= 23:
            raise ValueError("DEVICE-008 future_schedule_prepare_hour_local 不合法")
        if type(lead_minutes) is not int or not 0 <= lead_minutes <= 120:
            raise ValueError("DEVICE-008 prefetch_lead_minutes 不合法")
        if type(server_margin_minutes) is not int or not 0 <= server_margin_minutes <= 60:
            raise ValueError("DEVICE-008 server_prefetch_margin_minutes 不合法")
        strategy, normalized_sync_time = normalize_sync_strategy(sync_strategy, sync_time)
        slots = validate_offline_schedule(
            schedule, maximum=24, minimum_gap_minutes=minimum_gap_minutes
        )
        zone = local_now.tzinfo
        if zone is None:
            raise ValueError("DEVICE-008 裝置時間必須包含時區")
        tomorrow = local_now.date() + timedelta(days=1)
        configured = datetime.combine(
            local_now.date(), clock_time(future_prepare_hour, 0), tzinfo=zone
        )
        if strategy == "fixed_daily":
            assert normalized_sync_time is not None
            hour, minute = (int(part) for part in normalized_sync_time.split(":"))
            technical = datetime.combine(tomorrow, clock_time(hour, minute), tzinfo=zone)
        else:
            hour, minute = (int(part) for part in slots[0].split(":"))
            technical = datetime.combine(
                tomorrow, clock_time(hour, minute), tzinfo=zone
            ) - timedelta(minutes=lead_minutes + server_margin_minutes)
        return local_now >= min(configured, technical)

    @classmethod
    def _offline_prefetch_target_dates(
        cls,
        local_now: datetime,
        schedule: list[str],
        lead_minutes: int,
        server_margin_minutes: int = 0,
        future_prepare_hour: int = 20,
        sync_strategy: str = "first_display_lead",
        sync_time: str | None = None,
        minimum_gap_minutes: int = MINIMUM_SCHEDULE_GAP_MINUTES,
    ) -> list[date]:
        """Return independent today/tomorrow preparation targets in order."""

        targets: list[date] = []
        today = cls._offline_prefetch_target_date(
            local_now,
            schedule,
            lead_minutes,
            server_margin_minutes,
            sync_strategy,
            sync_time,
            minimum_gap_minutes,
        )
        if today is not None:
            targets.append(today)
        if cls._offline_tomorrow_prefetch_due(
            local_now,
            schedule,
            lead_minutes,
            server_margin_minutes,
            future_prepare_hour,
            sync_strategy,
            sync_time,
            minimum_gap_minutes,
        ):
            tomorrow = local_now.date() + timedelta(days=1)
            if tomorrow not in targets:
                targets.append(tomorrow)
        return targets

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
                    json.loads(str(device["schedule_times_json"] or "[]")),
                    maximum=24,
                    minimum_gap_minutes=int(
                        device["minimum_schedule_gap_minutes"] or MINIMUM_SCHEDULE_GAP_MINUTES
                    ),
                )
                targets = self._offline_prefetch_target_dates(
                    local_now,
                    schedule,
                    int(device["prefetch_lead_minutes"] or 0),
                    server_margin,
                    future_prepare_hour,
                    str(device["sync_strategy"] or "first_display_lead"),
                    device["sync_time"],
                    int(device["minimum_schedule_gap_minutes"] or MINIMUM_SCHEDULE_GAP_MINUTES),
                )
                for target_date in targets[:2]:
                    target_iso = target_date.isoformat()
                    if offline_schedules.ready_for_device(
                        device_id=str(device["id"]),
                        target_date=target_iso,
                        config_version=int(device["config_version"]),
                    ) is not None:
                        continue
                    terminal_outcome = offline_schedules.terminal_outcome_for_device(
                        device_id=str(device["id"]),
                        target_date=target_iso,
                        config_version=int(device["config_version"]),
                    )
                    if terminal_outcome is not None and not offline_schedules.claim_terminal_outcome_retry(
                        terminal_outcome=terminal_outcome,
                        device_id=str(device["id"]),
                        target_date=target_iso,
                        config_version=int(device["config_version"]),
                        now=now,
                    ):
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

    def _enqueue_task(
        self,
        task: dict,
        now: datetime,
        *,
        force: bool = False,
        trigger_source: str = "scheduler",
    ) -> None:
        is_scheduled = trigger_source == "scheduler"
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
        dedupe_key = f"scheduled:{task['key']}" if is_scheduled else None
        common = {
            "timeout_seconds": int(task["timeout_seconds"]),
            "trigger_source": trigger_source,
        }
        if is_scheduled:
            common.update(
                scheduled_task=task["key"],
                max_retries=int(task["retry_count"]),
                max_attempts=int(task["retry_count"]) + 1,
                retry_interval_seconds=int(task["retry_interval_seconds"]),
            )
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
                    "trigger_source": trigger_source,
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
        if is_scheduled:
            self.app.extensions["inktime_schedule_repository"].mark_enqueued(task, now)
        log_event(
            LOGGER,
            logging.INFO,
            "已建立排程背景工作" if is_scheduled else "已建立手動背景工作",
            event="scheduled_task_enqueued" if is_scheduled else "manual_task_enqueued",
            job_id=job_id,
            details={
                "task": task["key"],
                "trigger_source": trigger_source,
                "priority": repository.get(job_id)["priority"],
            },
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
            try:
                self.tick()
            except Exception as exc:
                log_event(
                    LOGGER,
                    logging.ERROR,
                    "Scheduler 主迴圈發生未預期錯誤；將以有界延遲恢復",
                    event="scheduler_tick_failed",
                    error_code="SCHEDULE-003",
                    details={"error_type": exc.__class__.__name__},
                )
                self.stop.wait(30)
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
