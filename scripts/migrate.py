#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from inktime.app.db import Database, migrate


def main() -> None:
    parser = argparse.ArgumentParser(description="升級 InkTime 資料庫")
    parser.add_argument("--database", default="data/inktime.db")
    parser.add_argument("--backup-dir", default="data/backups")
    args = parser.parse_args()
    database = Database(Path(args.database))
    versions = migrate(database, Path(args.backup_dir))
    print(f"資料庫完整性：{database.integrity_check(full=True)}")
    print("已套用版本：" + (", ".join(map(str, versions)) if versions else "無"))


if __name__ == "__main__":
    main()
