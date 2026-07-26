from __future__ import annotations

import inspect
import multiprocessing
import pickle
import threading
import time
from typing import Any, Callable
from uuid import uuid4


class ProcessCallTimeout(TimeoutError):
    code = "AI-PROVIDER-TIMEOUT"


class ProcessCallError(RuntimeError):
    code = "AI-PROVIDER-UNAVAILABLE"


def _call_child(function: Callable[..., Any], kwargs: dict[str, Any], sender) -> None:
    try:
        sender.send(("ok", function(**kwargs)))
    except BaseException as exc:
        # Do not pass exception strings: SDK errors can contain URLs or request data.
        sender.send(("error", type(exc).__name__))
    finally:
        sender.close()


def _provider_child(
    specification: dict[str, Any], method: str, kwargs: dict[str, Any], sender
) -> None:
    provider = None
    try:
        if str(specification.get("provider_kind")) != "openai_compatible":
            raise ValueError("unsupported provider isolation")
        from inktime.app.providers.openai_compatible import OpenAICompatibleProvider

        provider = OpenAICompatibleProvider.from_process_spec(specification)
        sender.send(("ok", getattr(provider, method)(**kwargs)))
    except BaseException as exc:
        sender.send(("error", type(exc).__name__))
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

    def _terminate(self, process) -> None:
        if process.is_alive():
            process.terminate()
            with self._lock:
                self._metrics["terminated"] += 1
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
        receiver, sender = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=target,
            args=(*args, sender),
            name=process_name,
        )
        try:
            process.start()
            sender.close()
            with self._lock:
                self._active[token] = (process, receiver)
                self._metrics["active"] += 1
                self._metrics["active_max"] = max(
                    self._metrics["active_max"], self._metrics["active"]
                )
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
                raise ProcessCallError(str(value))
            return value
        finally:
            sender.close()
            receiver.close()
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
        return self._run(
            _call_child,
            (function, kwargs),
            timeout_seconds=timeout_seconds,
            process_name=process_name,
            cancel_requested=cancel_requested,
        )

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
        return self._run(
            _provider_child,
            (specification, method, kwargs),
            timeout_seconds=timeout_seconds,
            process_name="inktime-provider-child",
        )

    @property
    def start_method(self) -> str | None:
        return self._context.get_start_method() if self._context is not None else None

    def shutdown(self) -> None:
        with self._lock:
            active = list(self._active.values())
        for process, receiver in active:
            self._terminate(process)
            receiver.close()

    def record_cooperative(self) -> None:
        with self._lock:
            self._metrics["cooperative"] += 1

    def observability(self) -> dict[str, int]:
        with self._lock:
            return dict(self._metrics)
