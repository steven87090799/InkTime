#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from inktime.app.db import Database
from inktime.app.services.backups import BackupService


def main() -> None:
    parser = argparse.ArgumentParser(
        description="InkTime 離線備份還原；Web、Worker、Scheduler 必須全部停止"
    )
    parser.add_argument("backup", type=Path, help="inktime-backup-*.zip 或 pre-migration .sqlite3")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("INKTIME_DATABASE", "/data/inktime.db")),
    )
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--yes", action="store_true", help="確認執行原子還原")
    args = parser.parse_args()
    if not args.yes:
        parser.error("必須加上 --yes；執行前請先停止所有 InkTime 容器")
    backup = args.backup.expanduser().resolve()
    database_path = args.database.expanduser().resolve()
    if not backup.is_file():
        parser.error("找不到指定備份")
    if not database_path.is_file():
        parser.error("找不到目前 SQLite 資料庫")
    service = BackupService(
        Database(database_path),
        (args.backup_dir or database_path.parent / "backups").expanduser().resolve(),
    )
    result = (
        service.restore(backup)
        if backup.suffix.casefold() == ".zip"
        else service.restore_sqlite_snapshot(backup)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
