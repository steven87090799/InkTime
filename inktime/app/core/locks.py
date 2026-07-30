from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
import time
from types import ModuleType
from typing import ContextManager, IO, Iterator, Protocol


class LockUnavailableError(RuntimeError):
    """A cross-process lock is unsupported or could not be acquired in time."""


class FcntlAdapter:
    """Lazy OS boundary shared by every existing cross-process file lock."""

    _module: ModuleType | None = None

    def _load(self) -> ModuleType:
        if sys.platform == "win32":
            raise LockUnavailableError(
                "Windows Native 不支援 InkTime fcntl 跨程序鎖；請使用 Linux Docker Container"
            )
        if self._module is None:
            import fcntl as native_fcntl

            self._module = native_fcntl
        return self._module

    @property
    def LOCK_EX(self) -> int:
        return int(self._load().LOCK_EX)

    @property
    def LOCK_SH(self) -> int:
        return int(self._load().LOCK_SH)

    @property
    def LOCK_NB(self) -> int:
        return int(self._load().LOCK_NB)

    @property
    def LOCK_UN(self) -> int:
        return int(self._load().LOCK_UN)

    def flock(self, descriptor: int, operation: int) -> None:
        self._load().flock(descriptor, operation)


fcntl = FcntlAdapter()


class LockProvider(Protocol):
    def exclusive(self, path: Path, timeout_seconds: float = 10.0) -> ContextManager[IO[bytes]]: ...


class FcntlLockProvider:
    """Existing Linux/macOS cross-process lock behind an injectable interface."""

    @contextmanager
    def exclusive(self, path: Path, timeout_seconds: float = 10.0) -> Iterator[IO[bytes]]:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise LockUnavailableError(f"取得跨程序鎖逾時：{path.name}") from exc
                    time.sleep(0.02)
            yield handle
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
