from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import IO, Iterator


_WRITE_STATEMENT = re.compile(
    r"^\s*(?:BEGIN|COMMIT|ROLLBACK|INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|VACUUM|REINDEX|ANALYZE|ATTACH|DETACH)\b",
    re.IGNORECASE,
)
_WITH_WRITE = re.compile(r"\b(?:INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)


class RuntimeLockError(RuntimeError):
    """資料庫仍被 InkTime 程序使用，不能執行離線還原。"""


class ManagedConnection(sqlite3.Connection):
    """在 SQLite 本身的單一 writer 限制外，再加上跨程序公平寫入邊界。

    WAL reader 不會取得此鎖；第一個寫入 statement 取得鎖並持有至交易
    COMMIT／ROLLBACK。既有 Repository 即使仍使用 ``session()``，也不會讓
    Web、Worker 與 Scheduler 在不同程序任意競爭寫入。
    """

    _writer_lock_path: Path | None = None
    _writer_timeout_seconds: float = 10.0
    _writer_lock_file: IO[bytes] | None = None

    def configure_writer_lock(self, path: Path, timeout_seconds: float) -> None:
        self._writer_lock_path = path
        self._writer_timeout_seconds = timeout_seconds
        self._writer_guard = threading.RLock()

    @staticmethod
    def _requires_writer(sql: str) -> bool:
        if _WRITE_STATEMENT.search(sql):
            return True
        return sql.lstrip().upper().startswith("WITH") and bool(_WITH_WRITE.search(sql))

    def _acquire_writer(self) -> None:
        if self._writer_lock_file is not None:
            return
        if self._writer_lock_path is None:
            return
        self._writer_lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = self._writer_lock_path.open("a+b")
        deadline = time.monotonic() + self._writer_timeout_seconds
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._writer_lock_file = lock
                return
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    lock.close()
                    raise sqlite3.OperationalError("database writer lock timeout") from exc
                time.sleep(0.01)

    def _release_writer(self) -> None:
        lock = self._writer_lock_file
        if lock is None:
            return
        self._writer_lock_file = None
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()

    def _run_statement(self, method, sql: str, *args):
        requires_writer = self._requires_writer(sql)
        acquired_here = False
        guard = getattr(self, "_writer_guard", None)
        if guard is None:
            return method(sql, *args)
        with guard:
            if requires_writer and self._writer_lock_file is None:
                self._acquire_writer()
                acquired_here = True
            try:
                result = method(sql, *args)
            except Exception:
                if acquired_here and not self.in_transaction:
                    self._release_writer()
                raise
            if self._writer_lock_file is not None and not self.in_transaction:
                self._release_writer()
            return result

    def execute(self, sql: str, parameters=(), /):
        return self._run_statement(super().execute, sql, parameters)

    def executemany(self, sql: str, seq_of_parameters, /):
        return self._run_statement(super().executemany, sql, seq_of_parameters)

    def executescript(self, sql_script: str, /):
        return self._run_statement(super().executescript, sql_script)

    def commit(self) -> None:
        try:
            super().commit()
        finally:
            if not self.in_transaction:
                self._release_writer()

    def rollback(self) -> None:
        try:
            super().rollback()
        finally:
            if not self.in_transaction:
                self._release_writer()

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._release_writer()


class Database:
    """集中管理 SQLite 連線、WAL reader 與跨程序 single-writer 設定。"""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 10_000) -> None:
        self.path = path.expanduser().resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self.writer_lock_path = Path(f"{self.path}.writer.lock")
        self.runtime_lock_path = Path(f"{self.path}.runtime.lock")

    def connect(self) -> ManagedConnection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
            factory=ManagedConnection,
        )
        connection.configure_writer_lock(
            self.writer_lock_path, self.busy_timeout_ms / 1000
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """所有多步驟寫入共用的 rollback-safe 交易入口。"""

        with self.session() as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")

    def acquire_runtime_lock(self, *, exclusive: bool, blocking: bool = True) -> IO[bytes]:
        """正式程序持有 shared lock；離線還原必須取得 exclusive lock。"""

        self.runtime_lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.runtime_lock_path.open("a+b")
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(lock.fileno(), operation)
        except BlockingIOError as exc:
            lock.close()
            raise RuntimeLockError(
                "RESTORE-001 InkTime Web、Worker 或 Scheduler 尚未停止"
            ) from exc
        return lock

    def integrity_check(self, *, full: bool = False) -> str:
        pragma = "PRAGMA integrity_check" if full else "PRAGMA quick_check"
        with self.session() as connection:
            row = connection.execute(pragma).fetchone()
            return str(row[0]) if row else "unknown"

    def schema_version(self) -> int:
        with self.session() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if not exists:
                return 0
            row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
            return int(row[0]) if row else 0
