from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import logging
import threading
import time
from typing import Callable
from uuid import uuid4

from inktime.app.repositories.jobs import JobRepository
from inktime.app.core.logging import (
    log_context,
    log_event,
    should_log_rate_limited,
    should_log_sample,
)


Processor = Callable[[dict], dict]
ProgressCallback = Callable[[int], None]
ErrorCallback = Callable[[str, str, Exception, int], None]
LOGGER = logging.getLogger("worker")


class BoundedJobWorker:
    """只維持固定數量 Future；照片總數不會放大 Worker 記憶體。"""

    def __init__(
        self,
        repository: JobRepository,
        processor: Processor,
        *,
        concurrency: int = 2,
        queue_multiplier: int = 2,
        max_attempts: int = 3,
        progress_interval_items: int = 50,
        progress_interval_seconds: int = 300,
        progress_callback: ProgressCallback | None = None,
        error_callback: ErrorCallback | None = None,
        timeout_seconds: int = 0,
    ) -> None:
        self.repository = repository
        self.processor = processor
        self.concurrency = max(1, concurrency)
        self.queue_size = self.concurrency * max(1, queue_multiplier)
        self.max_attempts = max_attempts
        self.progress_interval_items = max(1, progress_interval_items)
        self.progress_interval_seconds = max(1, progress_interval_seconds)
        self.progress_callback = progress_callback
        self.error_callback = error_callback
        self.timeout_seconds = max(0, int(timeout_seconds))
        self.worker_id = str(uuid4())
        self.stop_event = threading.Event()
        self.max_observed_futures = 0
        self.processed_items = 0
        self.failure_count = 0
        self._last_progress_at = time.monotonic()

    def request_stop(self) -> None:
        self.stop_event.set()
        log_event(
            LOGGER,
            logging.INFO,
            "Worker shutdown requested",
            event="worker_shutdown_requested",
            worker_id=self.worker_id,
            details={"active_processed": self.processed_items},
        )

    def _process(self, job_id: str, item) -> tuple[str, dict, float]:
        with log_context(
            trace_id=job_id,
            operation_id=job_id,
            job_id=job_id,
            job_item_id=str(item["id"]),
            photo_id=str(item["photo_id"] or ""),
            worker_id=self.worker_id,
            attempt=int(item["attempts"] or 0),
            operation="worker_item",
        ):
            result = self.processor(dict(item))
            cost = float(result.pop("_actual_cost", 0) or 0)
            return str(item["id"]), result, cost

    def _record_failure(self, job_id: str, item_id: str, exc: Exception) -> None:
        self.failure_count += 1
        code = str(getattr(exc, "code", "JOB-003"))
        if code.startswith("BUDGET-"):
            self.repository.defer_item(item_id)
            self.repository.transition(
                job_id,
                {"running", "retrying"},
                "budget_exceeded",
                "budget_exceeded",
            )
            if self.error_callback:
                self.error_callback(job_id, item_id, exc, self.failure_count)
            log_event(
                LOGGER,
                logging.WARNING,
                "Worker item stopped by budget",
                event="worker_item_budget_exceeded",
                error_code=code,
                job_id=job_id,
                job_item_id=item_id,
                worker_id=self.worker_id,
                failure_class=type(exc).__name__,
                retryable=False,
            )
            return
        outcome = self.repository.fail_item(
            job_id, item_id, code, str(exc), max_attempts=self.max_attempts
        )
        terminal = bool(outcome["terminal"])
        if should_log_sample(
            self.failure_count - 1,
            first=3,
            every=self.progress_interval_items,
        ):
            log_event(
                LOGGER,
                logging.ERROR if terminal else logging.WARNING,
                "Worker item reached terminal failure"
                if terminal
                else "Worker item retry scheduled",
                event="worker_item_terminal_failure"
                if terminal
                else "worker_item_retry_scheduled",
                error_code=code,
                job_id=job_id,
                job_item_id=item_id,
                worker_id=self.worker_id,
                attempt=int(outcome["attempt"]),
                failure_class=type(exc).__name__,
                retryable=not terminal,
                details={"sampled_failure_count": self.failure_count},
            )
        if self.error_callback and (
            self.failure_count <= 3 or self.failure_count % self.progress_interval_items == 0
        ):
            self.error_callback(job_id, item_id, exc, self.failure_count)

    def _record_processed(self) -> None:
        self.processed_items += 1
        now = time.monotonic()
        should_report = self.processed_items % self.progress_interval_items == 0
        should_report = should_report or now - self._last_progress_at >= self.progress_interval_seconds
        if should_report and self.progress_callback:
            self.progress_callback(self.processed_items)
            self._last_progress_at = now

    def run_job(self, job_id: str) -> None:
        started = time.monotonic()
        with log_context(
            trace_id=job_id,
            operation_id=job_id,
            job_id=job_id,
            worker_id=self.worker_id,
            operation="worker_job",
        ):
            log_event(
                LOGGER,
                logging.DEBUG,
                "Worker job execution started",
                event="worker_job_execution_started",
                details={
                    "execution_mode": "thread",
                    "concurrency": self.concurrency,
                    "queue_depth": self.queue_size,
                    "timeout_seconds": self.timeout_seconds,
                },
            )
            log_event(
                LOGGER,
                logging.DEBUG,
                "Worker execution mode selected",
                event="worker_execution_mode_selected",
                details={"execution_mode": "thread"},
            )
            try:
                self._run_job(job_id)
            finally:
                log_event(
                    LOGGER,
                    logging.DEBUG,
                    "Worker job execution finished",
                    event="worker_job_finalize_completed",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    details={
                        "processed": self.processed_items,
                        "failures": self.failure_count,
                        "max_in_flight": self.max_observed_futures,
                    },
                )

    def _run_job(self, job_id: str) -> None:
        futures: dict[Future, tuple[str, float]] = {}
        timed_out: set[Future] = set()
        timeout_triggered = False
        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="inktime") as executor:
            while not self.stop_event.is_set() or futures:
                job = self.repository.get(job_id)
                if job is None or job["status"] in {
                    "cancelled",
                    "completed",
                    "completed_with_errors",
                    "failed",
                    "paused",
                    "budget_exceeded",
                }:
                    break

                if job["status"] == "pausing" and not futures:
                    self.repository.acknowledge_pause(job_id)
                    log_event(
                        LOGGER,
                        logging.INFO,
                        "Worker pause acknowledged",
                        event="worker_pause_acknowledged",
                    )
                    break

                budget = job["budget_limit"]
                if budget is not None and float(budget) > 0 and float(job["spent"]) >= float(budget):
                    self.repository.transition(
                        job_id, {"running", "retrying"}, "budget_exceeded", "budget_exceeded"
                    )
                    break

                if (
                    not timeout_triggered
                    and job["status"] in {"running", "retrying"}
                    and len(futures) < self.queue_size
                ):
                    sampled_claim = should_log_sample(
                        self.processed_items,
                        first=3,
                        every=self.progress_interval_items,
                    )
                    if sampled_claim:
                        log_event(
                            LOGGER,
                            logging.DEBUG,
                            "Worker claim started",
                            event="worker_claim_started",
                            details={
                                "active_count": len(futures),
                                "queue_depth": self.queue_size,
                            },
                        )
                    claimed = self.repository.claim(job_id, self.worker_id, self.queue_size - len(futures))
                    if sampled_claim:
                        log_event(
                            LOGGER,
                            logging.DEBUG,
                            "Worker claim completed",
                            event="worker_claim_completed" if claimed else "worker_claim_empty",
                            details={
                                "claimed": len(claimed),
                                "active_count": len(futures),
                            },
                        )
                    for item in claimed:
                        future = executor.submit(self._process, job_id, item)
                        futures[future] = (str(item["id"]), time.monotonic())
                    self.max_observed_futures = max(self.max_observed_futures, len(futures))

                if not futures:
                    if should_log_rate_limited(
                        f"worker-claim-empty:{job_id}", interval_seconds=60
                    ):
                        log_event(
                            LOGGER,
                            logging.DEBUG,
                            "Worker claim returned no runnable items",
                            event="worker_claim_empty",
                        )
                    if self.repository.finalize_if_done(job_id):
                        break
                    # 可能正在等待指數退避；單次執行先交還 Scheduler。
                    break

                done, _ = wait(futures, timeout=min(30, self.timeout_seconds or 30), return_when=FIRST_COMPLETED)
                if not done:
                    try:
                        renewed = self.repository.renew_leases(job_id, self.worker_id)
                    except Exception as exc:
                        log_event(
                            LOGGER,
                            logging.ERROR,
                            "Worker lease renewal failed",
                            event="worker_lease_renew_failed",
                            error_code="JOB-LEASE-001",
                            failure_class=type(exc).__name__,
                            retryable=True,
                            exc_info=(type(exc), exc, exc.__traceback__),
                        )
                        raise
                    if should_log_sample(
                        self.processed_items,
                        first=1,
                        every=self.progress_interval_items,
                    ):
                        log_event(
                            LOGGER,
                            logging.DEBUG,
                            "Worker leases renewed",
                            event="worker_lease_renewed",
                            details={"renewed": renewed, "active_count": len(futures)},
                        )
                    if self.timeout_seconds:
                        expired = [
                            future
                            for future, (_item_id, started) in futures.items()
                            if future not in timed_out
                            and time.monotonic() - started >= self.timeout_seconds
                        ]
                        for future in expired:
                            timed_out.add(future)
                            timeout_triggered = True
                            item_id, item_started = futures[future]
                            log_event(
                                LOGGER,
                                logging.WARNING,
                                "Worker thread timeout detected; cooperative shutdown requested",
                                event="worker_thread_timeout_detected",
                                error_code="JOB-004",
                                job_item_id=item_id,
                                duration_ms=int((time.monotonic() - item_started) * 1000),
                                ambiguous=True,
                                retryable=False,
                            )
                            # Thread 無法被安全強制終止；停止 claim、要求 cooperative
                            # cancellation，並持續追蹤 Future 到真正完成。
                            self.stop_event.set()
                        if expired:
                            log_event(
                                LOGGER,
                                logging.WARNING,
                                "Worker entered cooperative shutdown after thread timeout",
                                event="worker_thread_cooperative_shutdown",
                                error_code="JOB-004",
                                ambiguous=True,
                                details={"active_count": len(futures), "expired": len(expired)},
                            )
                    continue
                for future in done:
                    item_id, _started = futures.pop(future)
                    try:
                        completed_id, result, cost = future.result()
                    except Exception as exc:
                        self._record_failure(job_id, item_id, exc)
                    else:
                        if future in timed_out:
                            self.repository.record_late_completion(
                                job_id, completed_id, result, cost
                            )
                            log_event(
                                LOGGER,
                                logging.WARNING,
                                "Worker item completed after timeout",
                                event="worker_late_completion",
                                error_code="JOB-004",
                                job_item_id=completed_id,
                                duration_ms=int((time.monotonic() - _started) * 1000),
                                ambiguous=False,
                            )
                        else:
                            self.repository.complete_item(job_id, completed_id, result, cost)
                    self._record_processed()

            # 優雅停止：已送出的工作完成並記錄；不再 claim 新項目。
            if futures:
                log_event(
                    LOGGER,
                    logging.INFO,
                    "Worker shutdown drain started",
                    event="worker_shutdown_drain_started",
                    details={"active_count": len(futures)},
                )
            for future in list(futures):
                item_id, _started = futures[future]
                try:
                    completed_id, result, cost = future.result()
                except Exception as exc:
                    self._record_failure(job_id, item_id, exc)
                else:
                    if future in timed_out:
                        self.repository.record_late_completion(job_id, completed_id, result, cost)
                        log_event(
                            LOGGER,
                            logging.WARNING,
                            "Worker item completed after timeout during drain",
                            event="worker_late_completion",
                            error_code="JOB-004",
                            job_item_id=completed_id,
                            duration_ms=int((time.monotonic() - _started) * 1000),
                        )
                    else:
                        self.repository.complete_item(job_id, completed_id, result, cost)
                self._record_processed()
            job = self.repository.get(job_id)
            if job is not None and job["status"] == "pausing":
                self.repository.acknowledge_pause(job_id)
                log_event(
                    LOGGER,
                    logging.INFO,
                    "Worker pause acknowledged",
                    event="worker_pause_acknowledged",
                )
            log_event(
                LOGGER,
                logging.DEBUG,
                "Worker job finalize started",
                event="worker_job_finalize_started",
            )
            self.repository.finalize_if_done(job_id)
