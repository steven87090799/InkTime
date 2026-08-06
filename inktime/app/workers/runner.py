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
from inktime.app.domain.jobs.failure_policy import (
    FailureClass,
    classify_codes,
    failure_code,
)
from inktime.app.workers.job_worker import BoundedJobWorker
from inktime.app.workers.scanner import PhotoScanner
from inktime.app.domain.photos import PhotoPreprocessor
from inktime.app.services.analysis import AnalysisDisabledError, ProviderUnavailableError
from inktime.app.domain.analysis.execution_mode import permits_automatic_ai, permits_manual_ai


LOGGER = logging.getLogger("worker")


class WorkerRunner:
    IDLE_BACKOFF_SECONDS = (15.0, 30.0, 60.0)

    def __init__(self, app) -> None:
        self.app = app
        self.stop = threading.Event()
        self.current: BoundedJobWorker | None = None

    def request_stop(self, *_args) -> None:
        self.stop.set()
        self.app.extensions["inktime_process_boundary"].shutdown()
        if self.current:
            self.current.request_stop()

    def run_once(self) -> int:
        repository = self.app.extensions["inktime_job_repository"]
        # Stale lease recovery has one owner: SchedulerRunner.  A Worker only
        # claims work that Scheduler has made runnable, avoiding two processes
        # racing to requeue the same expired item.
        recovered = 0
        processed_jobs = 0
        for job in repository.iter_runnable():
            if self.stop.is_set() or job["status"] not in {"running", "retrying"}:
                continue
            # An analysis_batch Job is only a durable parent/progress record;
            # its remote work is owned by OpenAI and is never claimed here.
            if str(job["kind"]) == "analysis_batch":
                continue
            settings = json.loads(job["settings_json"])
            analysis_plan = (
                json.loads(str(job["analysis_spec_json"] or "{}")) if str(job["kind"]) == "analysis" else {}
            )
            provider = None
            provider_error: ProviderUnavailableError | None = None
            execution = str(
                (analysis_plan.get("ai_execution_policy") or {}).get("execution_mode", "automatic_ai")
            )
            may_build_provider = permits_automatic_ai(execution) or (
                bool(settings.get("force_ai", False)) and permits_manual_ai(execution)
            )
            if str(job["kind"]) == "analysis" and str(job["strategy"]) != "local" and may_build_provider:
                try:
                    provider = self.app.extensions["inktime_provider_service"].build_router(
                        analysis_plan.get("provider_route"),
                        scoring_rules=str(analysis_plan.get("scoring_rules") or ""),
                    )
                except ValueError as exc:
                    provider_error = ProviderUnavailableError(str(exc))
            analysis = self.app.extensions["inktime_analysis_service"]
            runtime_settings = self.app.extensions["inktime_settings_repository"]
            progress_items = int(runtime_settings.get("worker.progress_items", 50))
            progress_seconds = int(runtime_settings.get("worker.progress_seconds", 300))
            scanner_disk_batch_size = int(runtime_settings.get("scanner.disk_batch_size", 1000))
            scanner_write_batch_size = int(runtime_settings.get("scanner.write_batch_size", 200))
            scanner_missing_threshold_ratio = (
                float(runtime_settings.get("scanner.missing_threshold_percent", 10)) / 100
            )
            scanner_safety = {
                "max_file_bytes": int(runtime_settings.get("scanner.max_file_bytes", 200 * 1024 * 1024)),
                "max_pixels": int(runtime_settings.get("scanner.max_pixels", 60_000_000)),
                "max_edge_px": int(runtime_settings.get("scanner.max_edge_px", 12_000)),
                "thumbnail_capacity_check_interval": int(
                    runtime_settings.get("scanner.thumbnail_capacity_check_interval", 500)
                ),
                "thumbnail_max_bytes": int(
                    runtime_settings.get("thumbnail_cache.max_bytes", 5 * 1024 * 1024 * 1024)
                ),
                "thumbnail_retention_days": int(runtime_settings.get("thumbnail_cache.retention_days", 30)),
                "quality_policy_settings": {
                    key: runtime_settings.get(key, default)
                    for key, default in (
                        ("analysis.prefilter_enabled", True),
                        ("analysis.prefilter_screenshots", True),
                        ("analysis.prefilter_low_quality", True),
                        ("analysis.prefilter_sensitivity", "conservative"),
                        ("analysis.e6_prefilter_enabled", True),
                        ("analysis.e6_min_score", 25),
                    )
                },
            }

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
                    error_code=failure_code(exc),
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
                    cancellation_cached = bool(current_job is None or current_job["status"] == "cancelled")
                    last_cancel_check = now
                return cancellation_cached

            def processor(
                item,
                *,
                job=job,
                settings=settings,
                provider=provider,
                provider_error=provider_error,
                analysis=analysis,
                analysis_plan=analysis_plan,
                execution=execution,
                progress_items=progress_items,
                progress_seconds=progress_seconds,
                scanner_disk_batch_size=scanner_disk_batch_size,
                scanner_write_batch_size=scanner_write_batch_size,
                scanner_missing_threshold_ratio=scanner_missing_threshold_ratio,
                scanner_safety=scanner_safety,
                runtime_settings=runtime_settings,
            ):
                if job["kind"] == "analysis" and execution == "disabled":
                    raise AnalysisDisabledError("Frozen Analysis Plan 指定完全停用；工作項目已拒絕")
                if provider_error is not None:
                    raise provider_error
                if job["kind"] == "analysis_batch_import":
                    return self.app.extensions["inktime_batch_analysis_service"].import_batch(
                        str(settings["batch_id"]),
                        cleanup_only=bool(settings.get("cleanup_only", False)),
                    )
                if job["kind"] == "render_preview":
                    operation = str(settings.get("operation", ""))
                    started = time.perf_counter()
                    if operation == "compare":
                        result = self.app.extensions["inktime_render_workload_service"].compare(settings)
                    elif operation == "simulate":
                        result = self.app.extensions["inktime_render_workload_service"].simulate(settings)
                    elif operation == "test_release":
                        result = self.app.extensions["inktime_render_workload_service"].test_release(
                            settings,
                            {
                                "job_id": str(job["id"]),
                                "item_id": str(item["id"]),
                                "worker_id": str(item["worker_id"]),
                                "idempotency_key": str(item["idempotency_key"]),
                            },
                        )
                    elif operation == "library_preview":
                        service = self.app.extensions["inktime_render_service"]
                        render_cache = self.app.extensions["inktime_render_cache"]
                        result = self.app.extensions["inktime_render_workload_service"].library_preview(
                            settings,
                            {
                                "job_id": str(job["id"]),
                                "item_id": str(item["id"]),
                                "worker_id": str(item["worker_id"]),
                                "idempotency_key": str(item["idempotency_key"]),
                            },
                            render_service=service,
                            render_cache=render_cache,
                        )
                    elif operation == "dual_pair_compare":
                        result = self.app.extensions["inktime_render_workload_service"].dual_pair_compare(
                            settings,
                            {
                                "job_id": str(job["id"]),
                                "item_id": str(item["id"]),
                                "worker_id": str(item["worker_id"]),
                                "idempotency_key": str(item["idempotency_key"]),
                            },
                            render_service=self.app.extensions["inktime_render_service"],
                        )
                    elif operation == "history_test_release":
                        result = self.app.extensions["inktime_render_workload_service"].test_release(
                            settings,
                            {
                                "job_id": str(job["id"]),
                                "item_id": str(item["id"]),
                                "worker_id": str(item["worker_id"]),
                                "idempotency_key": str(item["idempotency_key"]),
                            },
                        )
                    else:
                        raise ValueError("RENDER-008 不支援的背景渲染工作")
                    result["render_duration_ms"] = int((time.perf_counter() - started) * 1000)
                    return result
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
                        )
                        / 100,
                        cancel_requested=scan_cancel_requested,
                        progress_callback=log_scan_progress,
                        progress_interval_items=progress_items,
                        progress_interval_seconds=progress_seconds,
                        **scanner_safety,
                    )
                if job["kind"] == "render":
                    offline_prepare = settings.get("offline_prepare")
                    if isinstance(offline_prepare, dict):
                        return self.app.extensions["inktime_display_preparation_service"].prepare_device_day(
                            device_id=str(offline_prepare["device_id"]),
                            target_date=str(offline_prepare["target_date"]),
                            created_by=str(job["created_by"] or "system"),
                            expected_config_version=(
                                int(offline_prepare["config_version"])
                                if offline_prepare.get("config_version") is not None
                                else None
                            ),
                        )
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
                            release = self.app.extensions["inktime_render_service"].publish(
                                *arguments, history=history
                            )
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
                        **scanner_safety,
                    )
                    photo_ids = self.app.extensions["inktime_photo_repository"].list_existing_photo_ids(
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
                            photo_id,
                            str(job["id"]),
                            "local",
                            "local",
                            "virtual-display-local",
                            {
                                "schema_version": 1,
                                "caption": "本機電子紙收件匣照片",
                                "types": ["其他"],
                                "memory_score": 50,
                                "beauty_score": 50,
                                "technical_quality_score": 50,
                                "emotion_score": 50,
                                "side_caption": "",
                                "should_keep": True,
                                "sensitive": False,
                                "reason": "本機無模型發布",
                            },
                            "{}",
                            ranking_score=50,
                            final_ranking_score=50,
                        )
                    release = self.app.extensions["inktime_render_service"].publish(
                        photo_ids,
                        str(job["created_by"] or "system"),
                        profile_keys=[str(settings["profile_key"])],
                    )
                    return {"scan": scan, "release": release}
                if job["kind"] == "backup":
                    path = self.app.extensions["inktime_backup_service"].create()
                    removed = self.app.extensions["inktime_backup_service"].enforce_retention(
                        max(0, int(settings.get("retention_days", 14)))
                    )
                    return {"backup": path.name, "removed": removed}
                if job["kind"] == "cleanup":
                    cache = self.app.extensions["inktime_thumbnail_cache"]
                    inventory = cache.inventory()
                    return cache.cleanup(
                        max_bytes=int(settings.get("max_bytes", 5 * 1024 * 1024 * 1024)),
                        retention_days=int(settings.get("retention_days", 30)),
                        active_hashes=self.app.extensions["inktime_photo_repository"].active_hashes_for(
                            [entry[4] for entry in inventory]
                        ),
                        inventory=inventory,
                    )
                if job["kind"] == "webhook":
                    return self.app.extensions["inktime_notification_service"].deliver_one(
                        int(settings["notification_id"])
                    )
                return analysis.analyze_photo(
                    photo_id=item["photo_id"],
                    job_id=job["id"],
                    provider=provider,
                    strategy=job["strategy"],
                    analysis_plan=analysis_plan,
                    force_ai=bool(settings.get("force_ai", False)),
                    force_actor=str(job["created_by"] or "system"),
                    force_recompute=bool(job["force_recompute"]),
                )

            def record_result(result: dict, *, job=job, settings=settings) -> None:
                if str(job["kind"]) != "render_preview":
                    return
                self.app.extensions["inktime_render_cache"].record_duration(
                    int(result.get("render_duration_ms", 0)), background=True
                )
                if str(settings.get("operation")) == "compare":
                    self.app.extensions["inktime_render_workload_service"].record_compare_cache(
                        bool(result.get("cache_hit"))
                    )

            self.current = BoundedJobWorker(
                repository,
                processor,
                concurrency=min(
                    int(
                        settings.get(
                            "concurrency",
                            self.app.extensions["inktime_settings_repository"].get("analysis.concurrency"),
                        )
                    ),
                    self.app.extensions["inktime_runtime_config"].worker_concurrency,
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
                result_callback=record_result,
                timeout_seconds=int(settings.get("timeout_seconds", 0) or 0),
                hard_timeout=False,
            )
            log_event(
                LOGGER,
                logging.INFO,
                "開始處理工作",
                event="job_started",
                job_id=job["id"],
                details={"recovered_items": recovered},
            )
            try:
                self.current.run_job(job["id"])
            finally:
                close_provider = getattr(provider, "close", None)
                if callable(close_provider):
                    close_provider()
            finished = repository.get(job["id"])
            if finished is not None:
                scheduled_task = settings.get("scheduled_task")
                if scheduled_task:
                    schedules = self.app.extensions["inktime_schedule_repository"]
                    codes = repository.failure_codes(str(job["id"]))
                    if str(finished["status"]) == "completed":
                        if codes and classify_codes(codes) == FailureClass.TERMINAL_NO_RETRY:
                            task = schedules.get(str(scheduled_task))
                            if task:
                                schedules.record_terminal(
                                    task,
                                    f"{','.join(codes)} 工作狀態：completed",
                                    datetime.now().astimezone(),
                                )
                        else:
                            schedules.record_success(str(scheduled_task))
                    elif str(finished["status"]) not in {"running", "retrying"}:
                        task = schedules.get(str(scheduled_task))
                        if task:
                            classification = classify_codes(codes)
                            message = (
                                f"{','.join(codes) or str(finished['status'])} "
                                f"工作狀態：{finished['status']}"
                            )
                            if classification == FailureClass.TERMINAL_NO_RETRY:
                                schedules.record_terminal(
                                    task, message, datetime.now().astimezone()
                                )
                            else:
                                schedules.record_failure(
                                    task, message, datetime.now().astimezone()
                                )
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
                        "worker_child_active": self.current.child_active,
                        "worker_child_timeout": self.current.child_timeouts,
                        "worker_child_terminated": self.current.child_terminated,
                    },
                )
            self.current = None
            processed_jobs += 1
        return processed_jobs

    def run_forever(self, poll_seconds: float | None = None) -> None:
        repository = self.app.extensions["inktime_settings_repository"]
        configure_logging(settings_repository=repository)
        log_event(LOGGER, logging.INFO, "背景 Worker 已啟動", event="worker_started")
        idle_index = 0
        while not self.stop.is_set():
            processed = self.run_once()
            if processed == 0:
                configure_logging(settings_repository=repository)
                # Idle polling is deliberately adaptive: a quiet NAS wakes at
                # 15s, then backs off to 30/60s and stays capped there.  Any completed Job
                # resets the cursor so manual work remains responsive.
                configured = (
                    float(poll_seconds)
                    if poll_seconds is not None
                    else float(repository.get("worker.poll_seconds", 15))
                )
                wait_seconds = max(
                    self.IDLE_BACKOFF_SECONDS[idle_index],
                    min(max(1.0, configured), self.IDLE_BACKOFF_SECONDS[-1]),
                )
                self.stop.wait(wait_seconds)
                idle_index = min(idle_index + 1, len(self.IDLE_BACKOFF_SECONDS) - 1)
            else:
                idle_index = 0
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
    from inktime.app.bootstrap import bootstrap_services
    from inktime.app.core.runtime_config import resolve_runtime_config

    container = bootstrap_services(resolve_runtime_config(), role="worker")
    runner = WorkerRunner(container)
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)
    try:
        if args.once:
            runner.run_once()
        elif args.drain:
            runner.run_drain()
        else:
            runner.run_forever()
    finally:
        container.close()


if __name__ == "__main__":
    main()
