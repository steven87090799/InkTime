from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import threading
from typing import Callable
from uuid import uuid4

from inktime.app.repositories.jobs import JobRepository


Processor = Callable[[dict], dict]


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
    ) -> None:
        self.repository = repository
        self.processor = processor
        self.concurrency = max(1, concurrency)
        self.queue_size = self.concurrency * max(1, queue_multiplier)
        self.max_attempts = max_attempts
        self.worker_id = str(uuid4())
        self.stop_event = threading.Event()
        self.max_observed_futures = 0

    def request_stop(self) -> None:
        self.stop_event.set()

    def _process(self, item) -> tuple[str, dict, float]:
        result = self.processor(dict(item))
        cost = float(result.pop("_actual_cost", 0) or 0)
        return str(item["id"]), result, cost

    def _record_failure(self, job_id: str, item_id: str, exc: Exception) -> None:
        code = str(getattr(exc, "code", "JOB-003"))
        if code.startswith("BUDGET-"):
            self.repository.defer_item(item_id)
            self.repository.transition(
                job_id,
                {"running", "retrying"},
                "budget_exceeded",
                "budget_exceeded",
            )
            return
        self.repository.fail_item(job_id, item_id, code, str(exc), max_attempts=self.max_attempts)

    def run_job(self, job_id: str) -> None:
        futures: dict[Future, str] = {}
        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="inktime") as executor:
            while not self.stop_event.is_set():
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

                if job["status"] in {"running", "retrying"} and len(futures) < self.queue_size:
                    claimed = self.repository.claim(job_id, self.worker_id, self.queue_size - len(futures))
                    for item in claimed:
                        future = executor.submit(self._process, item)
                        futures[future] = str(item["id"])
                    self.max_observed_futures = max(self.max_observed_futures, len(futures))

                if not futures:
                    if self.repository.finalize_if_done(job_id):
                        break
                    # 可能正在等待指數退避；單次執行先交還 Scheduler。
                    break

                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    item_id = futures.pop(future)
                    try:
                        completed_id, result, cost = future.result()
                    except Exception as exc:
                        self._record_failure(job_id, item_id, exc)
                    else:
                        self.repository.complete_item(job_id, completed_id, result, cost)

            # 優雅停止：已送出的工作完成並記錄；不再 claim 新項目。
            for future in list(futures):
                item_id = futures[future]
                try:
                    completed_id, result, cost = future.result()
                except Exception as exc:
                    self._record_failure(job_id, item_id, exc)
                else:
                    self.repository.complete_item(job_id, completed_id, result, cost)
            job = self.repository.get(job_id)
            if job is not None and job["status"] == "pausing":
                self.repository.acknowledge_pause(job_id)
            self.repository.finalize_if_done(job_id)
