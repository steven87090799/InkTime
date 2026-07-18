#!/usr/bin/env python3
"""無額外套件的 Worker／Scheduler 容器程序健康檢查。"""

from __future__ import annotations

from pathlib import Path
import sys


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
