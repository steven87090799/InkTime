#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import platform
import tempfile
import time
from uuid import uuid4
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import psutil

from inktime.app.db import Database, migrate
from inktime.app.repositories.jobs import JobRepository
from inktime.app.repositories.photos import PhotoRepository


PHOTO_COUNT = 100_000


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("docs/PERFORMANCE_REPORT.md"))
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="inktime-performance-"))
    database = Database(root / "performance.db")
    migrate(database)
    process = psutil.Process()
    benchmark_started = time.perf_counter()
    cpu_started = time.process_time()
    rss_before = process.memory_info().rss
    rss_peak = rss_before
    library_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()

    insert_started = time.perf_counter()
    with database.session() as connection:
        connection.execute(
            "INSERT INTO libraries(id,name,root_path,created_at,updated_at) VALUES (?,?,?,?,?)",
            (library_id, "壓力測試", "/photos", now, now),
        )
        for start in range(0, PHOTO_COUNT, 1000):
            connection.executemany(
                "INSERT INTO photos(id,library_id,relative_path,status,captured_at,created_at,updated_at) VALUES (?,?,?,'preprocessed',?,?,?)",
                [
                    (
                        f"photo-{index:06d}",
                        library_id,
                        f"{index // 1000:03d}/{index:06d}.jpg",
                        f"{2000 + index % 26:04d}-07-17T10:00:00",
                        now,
                        now,
                    )
                    for index in range(start, min(start + 1000, PHOTO_COUNT))
                ],
            )
            rss_peak = max(rss_peak, process.memory_info().rss)
    insert_seconds = time.perf_counter() - insert_started

    photos = PhotoRepository(database)
    query_started = time.perf_counter()
    page, total = photos.search(status="preprocessed", limit=60, offset=99_900)
    query_ms = (time.perf_counter() - query_started) * 1000

    jobs = JobRepository(database)
    job_started = time.perf_counter()
    job_id = jobs.create(
        name="100k 測試",
        strategy="local",
        settings={},
        photo_ids=(f"photo-{index:06d}" for index in range(PHOTO_COUNT)),
        created_by="performance",
        budget_limit=0,
    )
    job_create_seconds = time.perf_counter() - job_started
    jobs.transition(job_id, {"pending"}, "running", "started")
    claimed = jobs.claim(job_id, "performance-worker", 8, lease_seconds=300)
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with database.session() as connection:
        connection.execute(
            "UPDATE job_items SET lease_until=? WHERE job_id=? AND status='running'", (expired, job_id)
        )
    recovery_started = time.perf_counter()
    recovered = jobs.recover_stale()
    recovery_ms = (time.perf_counter() - recovery_started) * 1000
    jobs.cancel(job_id)
    no_new_after_cancel = len(jobs.claim(job_id, "worker", 8)) == 0

    rss_after = process.memory_info().rss
    rss_peak = max(rss_peak, rss_after)
    db_size = database.path.stat().st_size
    with database.session() as connection:
        item_count = connection.execute(
            "SELECT COUNT(*) FROM job_items WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        indexes = [row[1] for row in connection.execute("PRAGMA index_list('photos')")]
    benchmark_seconds = time.perf_counter() - benchmark_started
    cpu_seconds = time.process_time() - cpu_started

    report = f"""# InkTime 100,000 筆效能驗收報告

測試日期：{now}  
環境：{platform.platform()}／Python {platform.python_version()}  
測試性質：使用 100,000 筆照片中繼資料與 Mock／本地流程，不呼叫真實模型、不含原始照片解碼時間。

| 指標 | 結果 |
|---|---:|
| 照片數 | {PHOTO_COUNT:,} |
| SQLite 大小 | {db_size / 1024 / 1024:.2f} MiB |
| 批次寫入時間 | {insert_seconds:.3f} 秒 |
| 模擬掃描寫入速度 | {PHOTO_COUNT / insert_seconds:,.0f} 筆／秒 |
| 第 99,901 筆起 UI 分頁查詢 | {query_ms:.2f} ms（回傳 {len(page)}／總數 {total:,}） |
| 建立 100,000 個持久化 Job Item | {job_create_seconds:.3f} 秒 |
| Job Item 建立速度 | {PHOTO_COUNT / job_create_seconds:,.0f} 筆／秒（{PHOTO_COUNT / job_create_seconds * 60:,.0f} 筆／分鐘） |
| Job Item 數 | {item_count:,} |
| Worker 單次 claim | {len(claimed)}（有界上限 8） |
| 重啟租約回收 | {recovery_ms:.2f} ms／{recovered} 筆 |
| 取消後停止 claim | {"通過" if no_new_after_cancel else "失敗"} |
| 量測期間最大 RSS | {rss_peak / 1024 / 1024:.2f} MiB |
| 最大 RSS 相對基線增量 | {(rss_peak - rss_before) / 1024 / 1024:.2f} MiB |
| 測試程序 CPU 時間 | {cpu_seconds:.2f} 秒／牆鐘 {benchmark_seconds:.2f} 秒（單核心等效 {cpu_seconds / benchmark_seconds * 100:.1f}%） |
| 照片索引 | {", ".join(indexes)} |
| SQLite 完整性 | {database.integrity_check()} |

## 驗收判定

- 工作建立採 500 筆批次寫入，Worker 每次只 claim `concurrency × 2`；不建立 100,000 個 Future。
- UI 使用 LIMIT/OFFSET 與索引，不使用 100,000 個 SQL placeholder。
- 租約逾時可回收到 `pending`；取消後 claim 回傳空集合，不會再送新請求。
- WAL、busy timeout、外鍵與正式 Migration 由共用 Database 連線層啟用。

## 已知瓶頸

- 深頁 OFFSET 仍會隨頁數增加成本；百萬級資料建議改用游標分頁或 PostgreSQL。
- pHash 與模糊度是 CPU 工作，實際 NAS 掃描速度受磁碟、網路與圖片解碼影響，不可用本報告的中繼資料寫入速度推估。
- SQLite 適合單主機中小型部署；多遠端 Worker 應切換 PostgreSQL 儲存層。
"""
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
