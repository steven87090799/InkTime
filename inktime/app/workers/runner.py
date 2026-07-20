from __future__ import annotations

import argparse
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
        for job in repository.list(limit=100):
            if self.stop.is_set() or job["status"] not in {"running", "retrying"}:
                continue
            settings = json.loads(job["settings_json"])
            scoring_profile = self.app.extensions["inktime_scoring_repository"].current()
            provider = self.app.extensions["inktime_provider_service"].build_router()
            analysis = self.app.extensions["inktime_analysis_service"]
            runtime_settings = self.app.extensions["inktime_settings_repository"]
            progress_items = int(runtime_settings.get("worker.progress_items", 50))
            progress_seconds = int(runtime_settings.get("worker.progress_seconds", 300))

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
                        build_thumbnails=bool(settings.get("build_thumbnails", True)),
                        progress_callback=log_scan_progress,
                        progress_interval_items=progress_items,
                        progress_interval_seconds=progress_seconds,
                    )
                if job["kind"] == "render":
                    arguments = (
                        [str(value) for value in settings.get("photo_ids", [])],
                        str(job["created_by"] or "system"),
                    )
                    if "profile_keys" in settings:
                        return self.app.extensions["inktime_render_service"].publish(
                            *arguments,
                            profile_keys=[str(value) for value in settings["profile_keys"]],
                        )
                    return self.app.extensions["inktime_render_service"].publish(*arguments)
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
                        build_thumbnails=False,
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
                    release = self.app.extensions["inktime_render_service"].publish(
                        photo_ids,
                        str(job["created_by"] or "system"),
                        profile_keys=[str(settings["profile_key"])],
                    )
                    return {"scan": scan, "release": release}
                if job["kind"] == "backup":
                    path = self.app.extensions["inktime_backup_service"].create()
                    return {"backup": path.name}
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


def main() -> None:
    parser = argparse.ArgumentParser(description="InkTime 背景 Worker")
    parser.add_argument("--once", action="store_true", help="處理目前工作後結束")
    args = parser.parse_args()
    from server import app

    runner = WorkerRunner(app)
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)
    with app.app_context():
        if args.once:
            runner.run_once()
        else:
            runner.run_forever()


if __name__ == "__main__":
    main()
