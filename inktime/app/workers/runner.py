from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
import signal
import threading
import time
from pathlib import Path

from inktime.app.core.logging import configure_logging, log_event
from inktime.app.workers.job_worker import BoundedJobWorker
from inktime.app.workers.scanner import PhotoScanner
from inktime.app.domain.photos import PhotoPreprocessor


LOGGER = logging.getLogger("worker")


class WorkerRunner:
    def __init__(self, app) -> None:
        self.app = app
        self.stop = threading.Event()
        self.current: BoundedJobWorker | None = None
        self._last_recovery_at = 0.0

    def request_stop(self, *_args) -> None:
        self.stop.set()
        if self.current:
            self.current.request_stop()

    def run_once(self) -> int:
        repository = self.app.extensions["inktime_job_repository"]
        recovered = 0
        if time.monotonic() - self._last_recovery_at >= 60:
            recovered = repository.recover_stale()
            self._last_recovery_at = time.monotonic()
        processed_jobs = 0
        for job in repository.iter_runnable():
            if self.stop.is_set() or job["status"] not in {"running", "retrying"}:
                continue
            settings = json.loads(job["settings_json"])
            scoring_profile = self.app.extensions["inktime_scoring_repository"].current()
            provider = self.app.extensions["inktime_provider_service"].build_router()
            analysis = self.app.extensions["inktime_analysis_service"]
            runtime_settings = self.app.extensions["inktime_settings_repository"]
            progress_items = int(runtime_settings.get("worker.progress_items", 50))
            progress_seconds = int(runtime_settings.get("worker.progress_seconds", 300))
            scanner_disk_batch_size = int(
                runtime_settings.get("scanner.disk_batch_size", 1000)
            )
            scanner_write_batch_size = int(
                runtime_settings.get("scanner.write_batch_size", 500)
            )
            scanner_missing_threshold_ratio = (
                float(runtime_settings.get("scanner.missing_threshold_percent", 10)) / 100
            )

            def log_progress(_processed_since_start: int, *, job_id=str(job["id"])) -> None:
                current = repository.get(job_id)
                if current is None:
                    return
                log_event(
                    LOGGER,
                    logging.INFO,
                    "工作進度更新",
                    event="job_progress",
                    job_id=job_id,
                    details={
                        "completed": int(current["completed_items"]),
                        "failed": int(current["failed_items"]),
                        "total": int(current["total_items"]),
                    },
                )

            def log_failure(
                failed_job_id: str,
                item_id: str,
                exc: Exception,
                failure_count: int,
            ) -> None:
                log_event(
                    LOGGER,
                    logging.ERROR,
                    "工作項目處理失敗；詳細內容已寫入錯誤中心",
                    event="job_item_failed",
                    error_code=str(getattr(exc, "code", "JOB-003")),
                    job_id=failed_job_id,
                    details={"item_id": item_id, "sampled_failure_count": failure_count},
                )

            def log_scan_progress(scan: dict, *, job_id=str(job["id"])) -> None:
                if self.current is not None:
                    repository.renew_leases(job_id, self.current.worker_id)
                log_event(
                    LOGGER,
                    logging.INFO,
                    "照片掃描進度更新",
                    event="scan_progress",
                    job_id=job_id,
                    details=scan,
                )

            last_cancel_check = 0.0
            cancellation_cached = False

            def scan_cancel_requested(*, job_id=str(job["id"])) -> bool:
                nonlocal last_cancel_check, cancellation_cached
                if self.stop.is_set() or (self.current is not None and self.current.stop_event.is_set()):
                    return True
                now = time.monotonic()
                if now - last_cancel_check >= 1.0:
                    current_job = repository.get(job_id)
                    cancellation_cached = bool(
                        current_job is None or current_job["status"] == "cancelled"
                    )
                    last_cancel_check = now
                return cancellation_cached

            def processor(
                item,
                *,
                job=job,
                settings=settings,
                provider=provider,
                analysis=analysis,
                scoring_profile=scoring_profile,
                progress_items=progress_items,
                progress_seconds=progress_seconds,
                scanner_disk_batch_size=scanner_disk_batch_size,
                scanner_write_batch_size=scanner_write_batch_size,
                scanner_missing_threshold_ratio=scanner_missing_threshold_ratio,
            ):
                if job["kind"] == "scan":
                    scanner = PhotoScanner(
                        self.app.extensions["inktime_photo_repository"],
                        PhotoPreprocessor(),
                        self.app.extensions["inktime_thumbnail_cache"],
                    )
                    return scanner.scan(
                        settings.get("library_name", "主要照片庫"),
                        Path(settings["root_path"]),
                        mode=str(settings.get("mode", "incremental")),
                        trigger_source=str(settings.get("trigger_source", "api")),
                        build_thumbnails=bool(settings.get("build_thumbnails", True)),
                        disk_batch_size=int(settings.get("disk_batch_size", scanner_disk_batch_size)),
                        write_batch_size=scanner_write_batch_size,
                        missing_threshold_ratio=float(
                            settings.get("missing_threshold_percent", scanner_missing_threshold_ratio * 100)
                        ) / 100,
                        cancel_requested=scan_cancel_requested,
                        progress_callback=log_scan_progress,
                        progress_interval_items=progress_items,
                        progress_interval_seconds=progress_seconds,
                    )
                if job["kind"] == "render":
                    display_prepare = settings.get("display_prepare")
                    if display_prepare is not None:
                        return self.app.extensions["inktime_display_preparation_service"].prepare(
                            display_prepare,
                            created_by=str(job["created_by"] or "system"),
                        )
                    arguments = (
                        [str(value) for value in settings.get("photo_ids", [])],
                        str(job["created_by"] or "system"),
                    )
                    history = settings.get("history")
                    if "profile_keys" in settings or "device_ids" in settings:
                        kwargs = {}
                        if "profile_keys" in settings:
                            kwargs["profile_keys"] = [str(value) for value in settings["profile_keys"]]
                        if "device_ids" in settings:
                            kwargs["device_ids"] = [str(value) for value in settings["device_ids"]]
                        if isinstance(history, dict):
                            kwargs["history"] = history
                        release = self.app.extensions["inktime_render_service"].publish(*arguments, **kwargs)
                    else:
                        if isinstance(history, dict):
                            release = self.app.extensions["inktime_render_service"].publish(*arguments, history=history)
                        else:
                            release = self.app.extensions["inktime_render_service"].publish(*arguments)
                    return release
                if job["kind"] == "virtual_display":
                    root = Path(settings["root_path"]).expanduser().resolve()
                    scanner = PhotoScanner(
                        self.app.extensions["inktime_photo_repository"],
                        PhotoPreprocessor(),
                        self.app.extensions["inktime_thumbnail_cache"],
                    )
                    scan = scanner.scan(
                        settings.get("library_name", "電子紙模擬照片"),
                        root,
                        mode="incremental",
                        trigger_source="virtual-display",
                        build_thumbnails=False,
                        disk_batch_size=scanner_disk_batch_size,
                        write_batch_size=scanner_write_batch_size,
                        missing_threshold_ratio=scanner_missing_threshold_ratio,
                        cancel_requested=scan_cancel_requested,
                        progress_callback=log_scan_progress,
                        progress_interval_items=progress_items,
                        progress_interval_seconds=progress_seconds,
                    )
                    photo_ids = self.app.extensions[
                        "inktime_photo_repository"
                    ].list_existing_photo_ids(
                        str(scan["library_id"]),
                        root,
                        limit=int(settings.get("quantity", 5)),
                    )
                    if not photo_ids:
                        raise ValueError("IMG-002 模擬照片資料夾內沒有可用圖片")
                    candidate_repository = self.app.extensions["inktime_render_candidate_repository"]
                    photo_repository = self.app.extensions["inktime_photo_repository"]
                    for photo_id in photo_ids:
                        if candidate_repository.get(photo_id) is not None:
                            continue
                        photo_repository.save_analysis(
                            photo_id, str(job["id"]), "local", "local", "virtual-display-local",
                            {
                                "schema_version": 1, "caption": "本機電子紙收件匣照片",
                                "types": ["其他"], "memory_score": 50, "beauty_score": 50,
                                "technical_quality_score": 50, "emotion_score": 50,
                                "side_caption": "", "should_keep": True, "sensitive": False,
                                "reason": "本機無模型發布",
                            },
                            "{}", ranking_score=50, final_ranking_score=50,
                        )
                    release = self.app.extensions["inktime_render_service"].publish(
                        photo_ids,
                        str(job["created_by"] or "system"),
                        profile_keys=[str(settings["profile_key"])],
                    )
                    return {"scan": scan, "release": release}
                if job["kind"] == "backup":
                    path = self.app.extensions["inktime_backup_service"].create()
                    return {"backup": path.name}
                if job["kind"] == "cleanup":
                    with self.app.extensions["inktime_database"].session() as connection:
                        hashes = {
                            str(row[0]).casefold()
                            for row in connection.execute(
                                "SELECT DISTINCT sha256 FROM photos WHERE lifecycle_status='active' AND sha256 IS NOT NULL"
                            )
                        }
                    cache = self.app.extensions["inktime_thumbnail_cache"]
                    return cache.cleanup(
                        max_bytes=int(settings.get("max_bytes", 5 * 1024 * 1024 * 1024)),
                        retention_days=int(settings.get("retention_days", 30)),
                        active_hashes=hashes,
                    )
                return analysis.analyze_photo(
                    photo_id=item["photo_id"],
                    job_id=job["id"],
                    provider=provider,
                    strategy=job["strategy"],
                    low_model=settings.get(
                        "low_model", self.app.extensions["inktime_settings_repository"].get("model.low_model")
                    ),
                    high_model=settings.get(
                        "high_model",
                        self.app.extensions["inktime_settings_repository"].get("model.high_model"),
                    ),
                    stage_two_threshold=float(
                        settings.get(
                            "stage_two_threshold",
                            self.app.extensions["inktime_settings_repository"].get(
                                "analysis.stage_two_threshold"
                            ),
                        )
                    ),
                    ranking_weights={
                        "memory": float(scoring_profile["memory_weight"]),
                        "beauty": float(scoring_profile["beauty_weight"]),
                        "technical_quality": float(scoring_profile["technical_weight"]),
                        "emotion": float(scoring_profile["emotion_weight"]),
                    },
                    favorite_bonus=float(scoring_profile["favorite_bonus"]),
                    scoring_version_id=str(scoring_profile["id"]),
                    force_ai=bool(settings.get("force_ai", False)),
                )

            self.current = BoundedJobWorker(
                repository,
                processor,
                concurrency=int(
                    settings.get(
                        "concurrency",
                        self.app.extensions["inktime_settings_repository"].get("analysis.concurrency"),
                    )
                ),
                queue_multiplier=int(runtime_settings.get("worker.queue_multiplier", 1)),
                max_attempts=int(
                    settings.get(
                        "max_retries",
                        self.app.extensions["inktime_settings_repository"].get("analysis.max_retries"),
                    )
                ),
                progress_interval_items=progress_items,
                progress_interval_seconds=progress_seconds,
                progress_callback=log_progress,
                error_callback=log_failure,
                timeout_seconds=int(settings.get("timeout_seconds", 0) or 0),
            )
            log_event(
                LOGGER,
                logging.INFO,
                "開始處理工作",
                event="job_started",
                job_id=job["id"],
                details={"recovered_items": recovered},
            )
            self.current.run_job(job["id"])
            finished = repository.get(job["id"])
            if finished is not None:
                scheduled_task = settings.get("scheduled_task")
                if scheduled_task:
                    schedules = self.app.extensions["inktime_schedule_repository"]
                    if str(finished["status"]) == "completed":
                        schedules.record_success(str(scheduled_task))
                    elif str(finished["status"]) not in {"running", "retrying"}:
                        task = schedules.get(str(scheduled_task))
                        if task:
                            schedules.record_failure(task, str(finished["status"]), datetime.now().astimezone())
                level = logging.WARNING if int(finished["failed_items"]) else logging.INFO
                log_event(
                    LOGGER,
                    level,
                    "工作處理告一段落",
                    event="job_finished",
                    job_id=job["id"],
                    details={
                        "status": str(finished["status"]),
                        "completed": int(finished["completed_items"]),
                        "failed": int(finished["failed_items"]),
                        "total": int(finished["total_items"]),
                        "max_in_flight": self.current.max_observed_futures,
                    },
                )
            self.current = None
            processed_jobs += 1
        return processed_jobs

    def run_forever(self, poll_seconds: float | None = None) -> None:
        repository = self.app.extensions["inktime_settings_repository"]
        configure_logging(settings_repository=repository)
        log_event(LOGGER, logging.INFO, "背景 Worker 已啟動", event="worker_started")
        while not self.stop.is_set():
            if self.run_once() == 0:
                configure_logging(settings_repository=repository)
                wait_seconds = (
                    float(poll_seconds)
                    if poll_seconds is not None
                    else float(repository.get("worker.poll_seconds", 15))
                )
                self.stop.wait(max(1.0, min(wait_seconds, 300.0)))
        log_event(LOGGER, logging.INFO, "背景 Worker 已停止", event="worker_stopped")

    def run_drain(self) -> int:
        """處理目前可執行的 Queue 後退出，不進入閒置輪詢。"""
        processed = 0
        while not self.stop.is_set():
            count = self.run_once()
            processed += count
            if count == 0:
                return processed
        return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="InkTime 背景 Worker")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="單次檢查後結束")
    mode.add_argument("--drain", action="store_true", help="處理目前 Queue 後結束")
    args = parser.parse_args()
    from server import app

    runner = WorkerRunner(app)
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)
    with app.app_context():
        if args.once:
            runner.run_once()
        elif args.drain:
            runner.run_drain()
        else:
            runner.run_forever()


if __name__ == "__main__":
    main()
