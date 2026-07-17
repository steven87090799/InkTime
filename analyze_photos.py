#!/usr/bin/env python3
"""舊命令名稱的相容入口；實際工作走 InkTime 2.x 資料庫 Job 與 Worker。"""

from __future__ import annotations

import argparse
from pathlib import Path

from inktime.app.workers.runner import WorkerRunner


def main() -> None:
    parser = argparse.ArgumentParser(description="建立 InkTime 照片掃描／分析背景工作")
    parser.add_argument("--scan", type=Path, help="先掃描指定照片資料夾")
    parser.add_argument("--library-name", default="主要照片庫")
    parser.add_argument(
        "--strategy", choices=["local", "low_cost", "high_quality", "smart_two_stage"], default=None
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--budget", type=float, default=None)
    args = parser.parse_args()

    from server import app

    with app.app_context():
        repository = app.extensions["inktime_job_repository"]
        service = app.extensions["inktime_job_service"]
        if args.scan:
            scan_id = repository.create_maintenance(
                kind="scan",
                name=f"掃描 {args.library_name}",
                settings={
                    "root_path": str(args.scan.expanduser().resolve()),
                    "library_name": args.library_name,
                    "build_thumbnails": True,
                },
                created_by="cli",
            )
            service.start(scan_id)
            WorkerRunner(app).run_once()
            print(f"掃描工作完成：{scan_id}")

        strategy = args.strategy or app.extensions["inktime_settings_repository"].get("analysis.strategy")
        job_id = service.create_analysis_job(
            name="CLI 照片分析",
            strategy=strategy,
            settings={},
            created_by="cli",
            budget_limit=args.budget,
            limit=args.limit,
        )
        service.start(job_id)
        WorkerRunner(app).run_once()
        job = repository.get(job_id)
        print(f"分析工作：{job_id}")
        print(f"狀態：{job['status']}；完成 {job['completed_items']}；失敗 {job['failed_items']}")


if __name__ == "__main__":
    main()
