from __future__ import annotations

import multiprocessing
import threading
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


class KillableProcessBoundary:
    """Hard timeout for explicitly isolated external calls or pure computation."""

    def __init__(self, *, max_processes: int = 2, terminate_grace_seconds: float = 0.5):
        self.max_processes = max(1, int(max_processes))
        self.terminate_grace_seconds = max(0.05, float(terminate_grace_seconds))
        self._slots = threading.BoundedSemaphore(self.max_processes)
        self._lock = threading.Lock()
        self._active: dict[str, tuple[Any, Any]] = {}
        self._metrics = {"active": 0, "active_max": 0, "timeout": 0, "terminated": 0}
        self._context: Any
        try:
            self._context = multiprocessing.get_context("fork")
        except ValueError:
            self._context = None

    @property
    def supported(self) -> bool:
        return self._context is not None

    def _terminate(self, process) -> None:
        if process.is_alive():
            process.terminate()
            self._metrics["terminated"] += 1
        process.join(self.terminate_grace_seconds)
        if process.is_alive():
            process.kill()
            process.join(self.terminate_grace_seconds)
        else:
            process.join(0)

    def call(
        self,
        function: Callable[..., Any],
        *,
        timeout_seconds: float,
        kwargs: dict[str, Any],
    ) -> Any:
        timeout = max(0.05, float(timeout_seconds))
        if self._context is None:
            # Spawn-only platforms keep the provider's cooperative HTTP timeout;
            # local closures cannot honestly be advertised as hard-killable.
            return function(**kwargs)
        if not self._slots.acquire(timeout=timeout):
            raise ProcessCallTimeout("provider process capacity timeout")
        token = uuid4().hex
        receiver, sender = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=_call_child,
            args=(function, kwargs, sender),
            name="inktime-provider-child",
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
            if not receiver.poll(timeout):
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

    def shutdown(self) -> None:
        with self._lock:
            active = list(self._active.values())
        for process, receiver in active:
            self._terminate(process)
            receiver.close()

    def observability(self) -> dict[str, int]:
        with self._lock:
            return dict(self._metrics)
