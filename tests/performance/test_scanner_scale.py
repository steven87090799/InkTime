from __future__ import annotations

from math import ceil
from pathlib import Path
from types import SimpleNamespace
import time
import tracemalloc

import pytest

from inktime.app.db import Database, migrate
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.workers.scanner import PhotoScanner


class CountingDatabase(Database):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.signature_selects = 0

    def connect(self):
        connection = super().connect()

        def trace(statement: str) -> None:
            normalized = " ".join(statement.split()).casefold()
            if "from photos" in normalized and "relative_path in" in normalized:
                self.signature_selects += 1

        connection.set_trace_callback(trace)
        return connection


class VirtualPath:
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path

    def relative_to(self, _root):
        return self

    def as_posix(self) -> str:
        return self.relative_path

    def stat(self):
        return SimpleNamespace(st_size=123, st_mtime=456.0)

    def __str__(self) -> str:
        return self.relative_path


class MustNotPreprocess:
    def analyze(self, *_args, **_kwargs):
        raise AssertionError("未變更照片不得執行預處理")


class MustNotThumbnail:
    def get_or_create(self, *_args, **_kwargs):
        raise AssertionError("本效能測試已停用縮圖")


def seed(database: Database, library_id: str, count: int) -> None:
    with database.transaction() as connection:
        for start in range(0, count, 1_000):
            connection.executemany(
                """
                INSERT INTO photos(
                    id,library_id,relative_path,file_size,modified_time,sha256,status,
                    lifecycle_status,metadata_status,local_features_status,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,'preprocessed','active','complete','complete',datetime('now'),datetime('now'))
                """,
                [
                    (
                        f"photo-{index:06d}",
                        library_id,
                        f"{index // 1000:03d}/{index:06d}.jpg",
                        123,
                        456.0,
                        f"{index:064x}",
                    )
                    for index in range(start, min(start + 1_000, count))
                ],
            )


@pytest.mark.parametrize("count", [10_000, 100_000])
def test_unchanged_scale_scan_is_batched_and_memory_bounded(monkeypatch, tmp_path, count):
    database = CountingDatabase(tmp_path / "inktime.db")
    migrate(database)
    root = tmp_path / "photos"
    root.mkdir()
    repository = PhotoRepository(database)
    library_id = repository.ensure_library("大量照片", root)
    seed(database, library_id, count)

    def virtual_media(_root, *, on_error=None):
        del on_error
        for index in range(count):
            yield VirtualPath(f"{index // 1000:03d}/{index:06d}.jpg"), "image"

    monkeypatch.setattr("inktime.app.workers.scanner.iter_media", virtual_media)
    scanner = PhotoScanner(repository, MustNotPreprocess(), MustNotThumbnail())
    database.signature_selects = 0
    tracemalloc.start()
    started = time.perf_counter()

    result = scanner.scan(
        "大量照片",
        root,
        trigger_source="test",
        build_thumbnails=False,
        disk_batch_size=1_000,
        write_batch_size=500,
    )

    elapsed = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(
        f"scanner_scale count={count} elapsed={elapsed:.3f}s "
        f"peak_mib={peak / 1024 / 1024:.2f} signature_selects={database.signature_selects}"
    )
    assert result["checked"] == count
    assert result["skipped"] == count
    assert result["processed"] == 0
    assert result["failed"] == 0
    assert database.signature_selects <= ceil(count / 1_000) * ceil(1_000 / 400)
    assert peak < 64 * 1024 * 1024
    assert elapsed < 120
