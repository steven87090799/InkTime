from __future__ import annotations

from pathlib import Path


class AssetCollisionError(RuntimeError):
    """Modern and Legacy assets must never depend on loader ordering."""


def _relative_files(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }


def assert_no_asset_collisions(modern_root: Path, legacy_root: Path, *, kind: str) -> None:
    collisions = sorted(_relative_files(modern_root) & _relative_files(legacy_root))
    if collisions:
        sample = ", ".join(collisions[:5])
        raise AssetCollisionError(f"Modern/Legacy {kind} 名稱衝突：{sample}")
