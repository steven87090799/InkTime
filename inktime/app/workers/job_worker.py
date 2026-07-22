from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import threading
import time
from typing import Callable
from uuid import uuid4

from inktime.app.repositories.jobs import JobRepository


Processor = Callable[[dict], dict]
ProgressCallback = Callable[[int], None]
ErrorCallback = Callable[[str, str, Exception, int], None]


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

    def _process(self, item) -> tuple[str, dict, float]:
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
            return
        self.repository.fail_item(job_id, item_id, code, str(exc), max_attempts=self.max_attempts)
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
                    claimed = self.repository.claim(job_id, self.worker_id, self.queue_size - len(futures))
                    for item in claimed:
                        future = executor.submit(self._process, item)
                        futures[future] = (str(item["id"]), time.monotonic())
                    self.max_observed_futures = max(self.max_observed_futures, len(futures))

                if not futures:
                    if self.repository.finalize_if_done(job_id):
                        break
                    # 可能正在等待指數退避；單次執行先交還 Scheduler。
                    break

                done, _ = wait(futures, timeout=min(30, self.timeout_seconds or 30), return_when=FIRST_COMPLETED)
                if not done:
                    self.repository.renew_leases(job_id, self.worker_id)
                    if self.timeout_seconds:
                        expired = [future for future, (_item_id, started) in futures.items() if time.monotonic() - started >= self.timeout_seconds]
                        for future in expired:
                            timed_out.add(future)
                            timeout_triggered = True
                            # Thread 無法被安全強制終止；停止 claim、要求 cooperative
                            # cancellation，並持續追蹤 Future 到真正完成。
                            self.stop_event.set()
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
                        else:
                            self.repository.complete_item(job_id, completed_id, result, cost)
                    self._record_processed()

            # 優雅停止：已送出的工作完成並記錄；不再 claim 新項目。
            for future in list(futures):
                item_id, _started = futures[future]
                try:
                    completed_id, result, cost = future.result()
                except Exception as exc:
                    self._record_failure(job_id, item_id, exc)
                else:
                    if future in timed_out:
                        self.repository.record_late_completion(job_id, completed_id, result, cost)
                    else:
                        self.repository.complete_item(job_id, completed_id, result, cost)
                self._record_processed()
            job = self.repository.get(job_id)
            if job is not None and job["status"] == "pausing":
                self.repository.acknowledge_pause(job_id)
            self.repository.finalize_if_done(job_id)
