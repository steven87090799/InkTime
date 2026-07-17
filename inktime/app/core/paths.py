from __future__ import annotations

from pathlib import Path, PureWindowsPath
from urllib.parse import unquote


class UnsafePathError(PermissionError):
    code = "PATH-001"


def safe_join(base: Path, relative: str) -> Path:
    """解析並限制在 base 內；拒絕絕對、URL 編碼與跨平台逃逸路徑。"""
    decoded = unquote(str(relative or ""))
    # 重複編碼（例如 %252e%252e）也必須在進入檔案系統前完全解開。
    for _ in range(2):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value

    windows = PureWindowsPath(decoded)
    if Path(decoded).is_absolute() or windows.is_absolute() or windows.drive:
        raise UnsafePathError("要求的路徑超出允許範圍")
    if "\\" in decoded or "\x00" in decoded:
        raise UnsafePathError("要求的路徑格式不合法")

    base_resolved = base.expanduser().resolve()
    target = (base_resolved / decoded).resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError as exc:
        raise UnsafePathError("要求的路徑超出允許範圍") from exc
    return target
