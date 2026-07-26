#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import re
import stat


LEGACY_LOCK = re.compile(r"^\.[0-9a-f]{64}-(?:512|1024|1600)\.lock$")


def legacy_lock_candidates(root: Path) -> list[Path]:
    """Return regular old lock files only; never follow symlinks."""

    expanded = root.expanduser()
    if expanded.is_symlink():
        raise ValueError("縮圖 Cache 根目錄不可為 Symlink")
    root = expanded.resolve()
    if not root.is_dir():
        raise ValueError("縮圖 Cache 目錄不存在或不是目錄")
    result: list[Path] = []
    for path in root.iterdir():
        if not LEGACY_LOCK.fullmatch(path.name):
            continue
        metadata = path.lstat()
        if stat.S_ISREG(metadata.st_mode):
            result.append(path)
    return sorted(result, key=lambda path: path.name)


def cleanup(root: Path, *, confirmed: bool, services_stopped: bool) -> dict[str, object]:
    candidates = legacy_lock_candidates(root)
    if confirmed and not services_stopped:
        raise ValueError("正式清理前必須確認 Web、Worker、Scheduler 已全部停止")
    removed = 0
    if confirmed:
        for path in candidates:
            metadata = path.lstat()
            if stat.S_ISREG(metadata.st_mode) and LEGACY_LOCK.fullmatch(path.name):
                path.unlink()
                removed += 1
    return {
        "mode": "delete" if confirmed else "dry-run",
        "matched": len(candidates),
        "removed": removed,
        "sample": [path.name for path in candidates[:10]],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="離線清理 InkTime 舊版逐照片縮圖鎖；預設只預覽"
    )
    parser.add_argument("cache_dir", type=Path)
    parser.add_argument("--yes", action="store_true", help="實際刪除符合規則的舊鎖")
    parser.add_argument(
        "--services-stopped",
        action="store_true",
        help="確認 Web、Worker、Scheduler 均已停止且沒有程序持有舊鎖",
    )
    args = parser.parse_args()
    result = cleanup(
        args.cache_dir, confirmed=args.yes, services_stopped=args.services_stopped
    )
    print(
        f"mode={result['mode']} matched={result['matched']} removed={result['removed']} "
        f"sample={','.join(result['sample'])}"
    )


if __name__ == "__main__":
    main()
