from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
import multiprocessing
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from inktime.app.domain.jobs.failure_policy import (
    FailureClass,
    classify_failure,
    failure_code,
)
from inktime.app.repositories.jobs import JobRepository


Processor = Callable[[dict], dict]
ProgressCallback = Callable[[int], None]
ErrorCallback = Callable[[str, str, Exception, int], None]
ResultCallback = Callable[[dict], None]


class JobHardTimeoutError(TimeoutError):
    code = "JOB-004"


class JobChildError(RuntimeError):
    code = "JOB-003"


def _process_item(processor: Processor, item: dict, sender) -> None:
    """Child entrypoint: compute only and return data to the parent over a pipe."""

    try:
        result = processor(item)
        cost = float(result.pop("_actual_cost", 0) or 0)
        sender.send(("ok", str(item["id"]), result, cost))
    except BaseException as exc:  # child must always report a bounded diagnostic
        sender.send(("error", type(exc).__name__))
    finally:
        sender.close()


@dataclass
class _ChildTask:
    item_id: str
    process: Any
    receiver: Any
    started_at: float


class BoundedJobWorker:
    """只維持固定數量 Future；照片總數不會放大 Worker 記憶體。"""

    MAX_CHILD_PROCESSES = 4

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
        result_callback: ResultCallback | None = None,
        timeout_seconds: float = 0,
        hard_timeout: bool = False,
        terminate_grace_seconds: float = 0.5,
    ) -> None:
        requested_hard_timeout = bool(hard_timeout and float(timeout_seconds) > 0)
        self.repository = repository
        self.processor = processor
        self.concurrency = max(1, concurrency)
        if requested_hard_timeout:
            self.concurrency = min(self.concurrency, self.MAX_CHILD_PROCESSES)
        self.queue_size = self.concurrency * max(1, queue_multiplier)
        self.max_attempts = max_attempts
        self.progress_interval_items = max(1, progress_interval_items)
        self.progress_interval_seconds = max(1, progress_interval_seconds)
        self.progress_callback = progress_callback
        self.error_callback = error_callback
        self.result_callback = result_callback
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self.hard_timeout = requested_hard_timeout
        self.terminate_grace_seconds = max(0.05, float(terminate_grace_seconds))
        self.worker_id = str(uuid4())
        self.stop_event = threading.Event()
        self.max_observed_futures = 0
        self.processed_items = 0
        self.failure_count = 0
        self.child_active = 0
        self.child_timeouts = 0
        self.child_terminated = 0
        self.child_active_max = 0
        self._last_progress_at = time.monotonic()

    def request_stop(self) -> None:
        self.stop_event.set()

    def _process(self, item) -> tuple[str, dict, float]:
        result = self.processor(dict(item))
        cost = float(result.pop("_actual_cost", 0) or 0)
        return str(item["id"]), result, cost

    def _record_failure(self, job_id: str, item_id: str, exc: Exception) -> None:
        self.failure_count += 1
        code = failure_code(exc)
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
        # Deterministic business/configuration outcomes are dead-lettered on
        # their first claim.  Only the central policy may decide that a code is
        # terminal; transient failures retain the bounded exponential retry.
        attempts = 1 if classify_failure(exc) == FailureClass.TERMINAL_NO_RETRY else self.max_attempts
        self.repository.fail_item(job_id, item_id, code, str(exc), max_attempts=attempts)
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

    def child_observability(self) -> dict[str, int]:
        return {
            "active": self.child_active,
            "active_max": self.child_active_max,
            "timeouts": self.child_timeouts,
            "terminated": self.child_terminated,
        }

    def _stop_child(self, task: _ChildTask) -> None:
        process = task.process
        if process.is_alive():
            process.terminate()
            self.child_terminated += 1
        process.join(self.terminate_grace_seconds)
        if process.is_alive():
            process.kill()
            process.join(self.terminate_grace_seconds)
        else:
            # A second join reaps the process deterministically on all platforms.
            process.join(0)
        task.receiver.close()
        self.child_active = max(0, self.child_active - 1)

    def _run_process_job(self, job_id: str) -> None:
        """Run explicitly safe/picklable computation behind killable processes.

        The child never receives the repository. Only the parent claims, renews,
        retries and persists the returned result. Callers must opt in only for an
        external call or pure computation that has no database side effects.
        """

        try:
            context = multiprocessing.get_context("spawn")
        except ValueError:
            self.hard_timeout = False
            self.run_job(job_id)
            return

        tasks: dict[str, _ChildTask] = {}
        last_lease_renewal = time.monotonic()
        try:
            while not self.stop_event.is_set() or tasks:
                job = self.repository.get(job_id)
                if job is None or job["status"] not in {"running", "retrying", "pausing"}:
                    break
                if not self.stop_event.is_set() and job["status"] in {"running", "retrying"}:
                    claimed = self.repository.claim(job_id, self.worker_id, self.concurrency - len(tasks))
                    for row in claimed:
                        item = dict(row)
                        receiver, sender = context.Pipe(duplex=False)
                        process = context.Process(
                            target=_process_item,
                            args=(self.processor, item, sender),
                            name="inktime-bounded-child",
                        )
                        process.start()
                        sender.close()
                        item_id = str(item["id"])
                        tasks[item_id] = _ChildTask(item_id, process, receiver, time.monotonic())
                        self.child_active += 1
                        self.child_active_max = max(self.child_active_max, self.child_active)
                    self.max_observed_futures = max(self.max_observed_futures, len(tasks))

                progressed = False
                now = time.monotonic()
                for item_id, task in list(tasks.items()):
                    if task.receiver.poll():
                        try:
                            message = task.receiver.recv()
                        except EOFError:
                            message = ("error", "ChildExited")
                        task.process.join(self.terminate_grace_seconds)
                        if task.process.is_alive():
                            self._stop_child(task)
                        else:
                            task.process.join(0)
                            task.receiver.close()
                            self.child_active = max(0, self.child_active - 1)
                        tasks.pop(item_id, None)
                        if message[0] == "ok":
                            _state, completed_id, result, cost = message
                            if self.result_callback:
                                self.result_callback(result)
                            self.repository.complete_item(
                                job_id, str(completed_id), dict(result), float(cost)
                            )
                        else:
                            self._record_failure(job_id, item_id, JobChildError(str(message[1])))
                        self._record_processed()
                        progressed = True
                        continue
                    if now - task.started_at >= self.timeout_seconds:
                        self.child_timeouts += 1
                        self._stop_child(task)
                        tasks.pop(item_id, None)
                        self._record_failure(job_id, item_id, JobHardTimeoutError("child process timeout"))
                        self._record_processed()
                        progressed = True

                if now - last_lease_renewal >= min(30.0, self.timeout_seconds / 2):
                    self.repository.renew_leases(job_id, self.worker_id)
                    last_lease_renewal = now
                if not tasks:
                    if self.repository.finalize_if_done(job_id):
                        break
                    if not progressed:
                        # Retry backoff is persisted; return control to Scheduler.
                        break
                if not progressed:
                    self.stop_event.wait(0.02)
        finally:
            # Shutdown and every exceptional path terminate, join and reap all
            # children. Their late pipe results are deliberately never consumed.
            for item_id, task in list(tasks.items()):
                self._stop_child(task)
                self._record_failure(job_id, item_id, JobChildError("worker shutdown"))
            job = self.repository.get(job_id)
            if job is not None and job["status"] == "pausing":
                self.repository.acknowledge_pause(job_id)
            self.repository.finalize_if_done(job_id)

    def run_job(self, job_id: str) -> None:
        if self.hard_timeout:
            self._run_process_job(job_id)
            return
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

                done, _ = wait(
                    futures, timeout=min(30, self.timeout_seconds or 30), return_when=FIRST_COMPLETED
                )
                if not done:
                    self.repository.renew_leases(job_id, self.worker_id)
                    if self.timeout_seconds:
                        expired = [
                            future
                            for future, (_item_id, started) in futures.items()
                            if time.monotonic() - started >= self.timeout_seconds
                        ]
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
                            self.repository.record_late_completion(job_id, completed_id, result, cost)
                        else:
                            if self.result_callback:
                                self.result_callback(result)
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
                        if self.result_callback:
                            self.result_callback(result)
                        self.repository.complete_item(job_id, completed_id, result, cost)
                self._record_processed()
            job = self.repository.get(job_id)
            if job is not None and job["status"] == "pausing":
                self.repository.acknowledge_pause(job_id)
            self.repository.finalize_if_done(job_id)
