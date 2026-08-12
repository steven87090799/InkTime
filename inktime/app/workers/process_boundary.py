from __future__ import annotations

import inspect
import logging
import multiprocessing
import pickle
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from inktime.app.core.logging import log_event


LOGGER = logging.getLogger("process_boundary")


class ProcessCallTimeout(TimeoutError):
    code = "AI-PROVIDER-TIMEOUT"
    child_started: bool = False
    ambiguous: bool | None = None
    vision_started: bool | None = None
    request_started: bool | None = None
    operation_id: str = ""


class ProcessCallError(RuntimeError):
    code = "AI-PROVIDER-UNAVAILABLE"
    child_started: bool = False
    ambiguous: bool | None = None
    vision_started: bool | None = None
    request_started: bool | None = None
    operation_id: str = ""


def _log_boundary_failure(
    exc: ProcessCallTimeout | ProcessCallError,
    *,
    call_started: float,
    process_name: str,
    provider_call: bool,
) -> None:
    """Log final failure semantics after provider normalization, if any."""

    is_timeout = isinstance(exc, ProcessCallTimeout)
    ambiguous = bool(exc.ambiguous)
    request_started = bool(exc.request_started)
    retryable_codes = {"VLM-001", "VLM-002", "VLM-005", "VLM-007"}
    retryable = not ambiguous and (
        is_timeout
        or not request_started
        or (provider_call and str(getattr(exc, "code", "")) in retryable_codes)
    )
    log_event(
        LOGGER,
        logging.WARNING if is_timeout else logging.ERROR,
        "Isolated process call timed out" if is_timeout else "Isolated process call failed",
        event="boundary_call_timeout" if is_timeout else "boundary_call_error",
        error_code=str(getattr(exc, "code", "AI-PROVIDER-UNAVAILABLE")),
        operation_id=str(getattr(exc, "operation_id", "")),
        operation="process_boundary",
        duration_ms=int((time.monotonic() - call_started) * 1000),
        failure_class=type(exc).__name__,
        retryable=retryable,
        ambiguous=ambiguous,
        details={
            "child_started": bool(exc.child_started),
            "request_started": request_started,
            "vision_started": bool(exc.vision_started),
            "process_name": process_name[:128],
        },
    )


def _call_child(function: Callable[..., Any], kwargs: dict[str, Any], sender) -> None:
    try:
        sender.send(("ok", function(**kwargs)))
    except BaseException as exc:
        # Do not pass exception strings: SDK errors can contain URLs or request data.
        sender.send(("error", type(exc).__name__))
    finally:
        sender.close()


def _provider_child(specification: dict[str, Any], method: str, kwargs: dict[str, Any], sender) -> None:
    provider = None
    try:
        if str(specification.get("provider_kind")) != "openai_compatible":
            raise ValueError("unsupported provider isolation")
        from inktime.app.providers.openai_compatible import OpenAICompatibleProvider

        provider = OpenAICompatibleProvider.from_process_spec(specification)
        sender.send(("ok", getattr(provider, method)(**kwargs)))
    except BaseException as exc:
        # Keep only structured control metadata; never serialize provider
        # messages, URLs, request data, or credentials across the boundary.
        sender.send(
            (
                "error",
                {
                    "type": type(exc).__name__,
                    "code": getattr(exc, "code", None),
                    "ambiguous": bool(getattr(exc, "ambiguous", False)),
                    "vision_started": bool(getattr(exc, "vision_started", False)),
                    "request_started": bool(getattr(exc, "request_started", False)),
                },
            )
        )
    finally:
        if provider is not None:
            provider.close()
        sender.close()


class KillableProcessBoundary:
    """Hard timeout for explicitly isolated external calls or pure computation."""

    def __init__(self, *, max_processes: int = 2, terminate_grace_seconds: float = 0.5):
        self.max_processes = max(1, int(max_processes))
        self.terminate_grace_seconds = max(0.05, float(terminate_grace_seconds))
        self._slots = threading.BoundedSemaphore(self.max_processes)
        self._lock = threading.Lock()
        self._reap_lock = threading.Lock()
        self._active: dict[str, tuple[Any, Any]] = {}
        self._metrics = {
            "active": 0,
            "active_max": 0,
            "timeout": 0,
            "terminated": 0,
            "cooperative": 0,
        }
        self._context: Any
        try:
            self._context = multiprocessing.get_context("spawn")
        except ValueError:
            self._context = None

    @property
    def supported(self) -> bool:
        return self._context is not None

    @staticmethod
    def _safe_close(resource) -> None:
        if resource is None:
            return
        try:
            resource.close()
        except Exception:  # noqa: S110 -- cleanup must not mask the primary failure
            pass

    def _terminate(self, process) -> None:
        """Idempotently reap a started child, even during concurrent shutdown."""

        with self._reap_lock:
            if process.is_alive():
                process.terminate()
                with self._lock:
                    self._metrics["terminated"] += 1
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "Isolated child process was terminated",
                    event="boundary_process_terminated",
                    operation="process_boundary",
                    details={"process_name": str(getattr(process, "name", "isolated-child"))[:128]},
                )
            process.join(self.terminate_grace_seconds)
            if process.is_alive():
                process.kill()
                process.join(self.terminate_grace_seconds)
            else:
                process.join(0)

    def _run(
        self,
        target: Callable[..., None],
        args: tuple[Any, ...],
        *,
        timeout_seconds: float,
        process_name: str,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Any:
        timeout = max(0.05, float(timeout_seconds))
        if self._context is None:
            raise ProcessCallError("spawn process boundary unavailable")
        if not self._slots.acquire(timeout=timeout):
            raise ProcessCallTimeout("provider process capacity timeout")
        token = uuid4().hex
        call_started = time.monotonic()
        receiver = None
        sender = None
        process = None
        started = False
        registered = False
        try:
            log_event(
                LOGGER,
                logging.DEBUG,
                "Isolated process call started",
                event="boundary_call_start",
                operation_id=token,
                operation="process_boundary",
                details={"process_name": process_name[:128]},
            )
            receiver, sender = self._context.Pipe(duplex=False)
            process = self._context.Process(
                target=target,
                args=(*args, sender),
                name=process_name,
            )
            process.start()
            started = True
            self._safe_close(sender)
            sender = None
            with self._lock:
                self._active[token] = (process, receiver)
                self._metrics["active"] += 1
                self._metrics["active_max"] = max(self._metrics["active_max"], self._metrics["active"])
                registered = True
            deadline = time.monotonic() + timeout
            while not receiver.poll(min(0.1, max(0.0, deadline - time.monotonic()))):
                if cancel_requested is not None and cancel_requested():
                    self._terminate(process)
                    raise ProcessCallError("child process cancelled")
                if time.monotonic() >= deadline:
                    with self._lock:
                        self._metrics["timeout"] += 1
                    self._terminate(process)
                    raise ProcessCallTimeout("provider child process timeout")
            try:
                state, value = receiver.recv()
            except EOFError as exc:
                raise ProcessCallError("provider child exited") from exc
            process.join(self.terminate_grace_seconds)
            if process.is_alive():
                self._terminate(process)
            else:
                process.join(0)
            if state != "ok":
                if isinstance(value, dict):
                    error = ProcessCallError(str(value.get("type") or "provider child failed"))
                    child_code = value.get("code")
                    if isinstance(child_code, str) and child_code:
                        error.code = child_code
                    error.ambiguous = bool(value.get("ambiguous", False))
                    error.vision_started = bool(value.get("vision_started", False))
                    error.request_started = bool(value.get("request_started", False))
                    raise error
                raise ProcessCallError(str(value))
            log_event(
                LOGGER,
                logging.DEBUG,
                "Isolated process call completed",
                event="boundary_call_success",
                operation_id=token,
                operation="process_boundary",
                duration_ms=int((time.monotonic() - call_started) * 1000),
                details={"process_name": process_name[:128]},
            )
            return value
        except (ProcessCallTimeout, ProcessCallError) as exc:
            # A failed process start proves that the provider method never
            # ran.  Once the child is running, however, timeout/pipe failure
            # makes a remote Vision POST outcome unknowable.
            exc.child_started = bool(started)
            exc.operation_id = token
            raise
        finally:
            # Reap before closing IPC, removing observability state, or releasing
            # capacity. Parent-side poll/recv/cancel exceptions must not orphan a
            # still-running child.
            if started and process is not None:
                self._terminate(process)
            self._safe_close(sender)
            self._safe_close(receiver)
            if registered:
                with self._lock:
                    self._active.pop(token, None)
                    self._metrics["active"] = max(0, self._metrics["active"] - 1)
            self._slots.release()

    def call(
        self,
        function: Callable[..., Any],
        *,
        timeout_seconds: float,
        kwargs: dict[str, Any],
        cancel_requested: Callable[[], bool] | None = None,
        process_name: str = "inktime-isolated-child",
    ) -> Any:
        if inspect.ismethod(function) or "<locals>" in function.__qualname__:
            raise ProcessCallError("spawn boundary requires a module-level function")
        try:
            pickle.dumps((function, kwargs))
        except (pickle.PickleError, TypeError, AttributeError) as exc:
            raise ProcessCallError("spawn boundary arguments are not serializable") from exc
        call_started = time.monotonic()
        try:
            return self._run(
                _call_child,
                (function, kwargs),
                timeout_seconds=timeout_seconds,
                process_name=process_name,
                cancel_requested=cancel_requested,
            )
        except (ProcessCallTimeout, ProcessCallError) as exc:
            _log_boundary_failure(
                exc,
                call_started=call_started,
                process_name=process_name,
                provider_call=False,
            )
            raise

    def call_provider(
        self,
        specification: dict[str, Any],
        method: str,
        *,
        timeout_seconds: float,
        kwargs: dict[str, Any],
    ) -> Any:
        if str(specification.get("provider_kind")) != "openai_compatible":
            raise ProcessCallError("provider does not support hard kill")
        try:
            pickle.dumps((specification, method, kwargs))
        except (pickle.PickleError, TypeError, AttributeError) as exc:
            raise ProcessCallError("provider request is not serializable") from exc
        call_started = time.monotonic()
        try:
            return self._run(
                _provider_child,
                (specification, method, kwargs),
                timeout_seconds=timeout_seconds,
                process_name="inktime-provider-child",
            )
        except (ProcessCallTimeout, ProcessCallError) as exc:
            # Once an isolated analysis child has started, a timeout or
            # unexpected exit cannot prove that the remote vision POST was
            # never accepted.  Fail closed instead of sending the image to a
            # second Provider.  Serialization failures happen before _run and
            # therefore do not reach this branch.
            if method in {"analyze", "repair_json"}:
                # A structured provider exception is authoritative, including
                # explicit False values that prove the request was not handed
                # to the transport.  A killed/EOF child has no such metadata;
                # once that child started, fail closed because its request
                # state is unknowable.
                child_metadata = any(
                    value is not None
                    for value in (exc.vision_started, exc.request_started, exc.ambiguous)
                )
                if not child_metadata and exc.child_started:
                    exc.request_started = True
                    exc.ambiguous = True
                    if method == "analyze":
                        exc.vision_started = True
            _log_boundary_failure(
                exc,
                call_started=call_started,
                process_name="inktime-provider-child",
                provider_call=True,
            )
            raise

    @property
    def start_method(self) -> str | None:
        return self._context.get_start_method() if self._context is not None else None

    def shutdown(self) -> None:
        with self._lock:
            active = list(self._active.values())
        for process, receiver in active:
            self._terminate(process)
            self._safe_close(receiver)
        log_event(
            LOGGER,
            logging.INFO,
            "Process boundary shutdown completed",
            event="boundary_shutdown_completed",
            operation="process_boundary",
            details={"active_processes": len(active)},
        )

    def record_cooperative(self) -> None:
        with self._lock:
            self._metrics["cooperative"] += 1

    def observability(self) -> dict[str, int]:
        with self._lock:
            return dict(self._metrics)
