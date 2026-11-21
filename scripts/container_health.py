#!/usr/bin/env python3
"""無額外套件的 Worker／Scheduler 容器程序健康檢查。"""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from inktime.app.core.runtime_health import component_ready

TARGETS = {
    "worker": b"inktime.app.workers.runner",
    "scheduler": b"inktime.app.workers.scheduler",
}


def process_exists(target: bytes) -> bool:
    for command_path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if target in command_path.read_bytes():
                return True
        except OSError:
            continue
    return False


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    target = TARGETS.get(mode)
    if target is None:
        print("usage: container_health.py worker|scheduler", file=sys.stderr)
        return 2
    if not process_exists(target):
        print(f"{mode} process not found", file=sys.stderr)
        return 1
    database = Path(os.environ.get("INKTIME_DATABASE") or "/data/inktime.db").resolve()
    try:
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", timeout=2, uri=True)) as connection:
            if component_ready(connection, mode):
                return 0
    except sqlite3.Error:
        pass
    print(f"{mode} heartbeat missing or stale", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
