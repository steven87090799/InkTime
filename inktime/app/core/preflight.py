"""Fail-closed checks for the small set of production deployment boundaries.

The inspector intentionally does not try to make SQLite safe on network storage:
WAL and flock are retained, but a remote mount is refused before either is used.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse


UNSAFE_NETWORK_FILESYSTEMS = frozenset({"nfs", "nfs4", "cifs", "smbfs", "sshfs", "9p"})


class PreflightError(ValueError):
    """Deployment configuration must be corrected before startup."""


class OSAdapter(Protocol):
    def mountinfo(self) -> str: ...


class NativeOSAdapter:
    def mountinfo(self) -> str:
        try:
            return Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except OSError:
            return ""


def filesystem_for(path: Path, adapter: OSAdapter | None = None) -> str | None:
    """Return the deepest Linux mount type; an empty result is deliberately safe.

    macOS does not expose Linux mountinfo.  Tests can provide an adapter and
    production macOS deployments should use a local APFS data directory.
    """
    text = (adapter or NativeOSAdapter()).mountinfo()
    if not text:
        return None
    target = str(path.expanduser().resolve())
    best: tuple[int, str] | None = None
    for line in text.splitlines():
        before, marker, after = line.partition(" - ")
        fields, fs_fields = before.split(), after.split()
        if not marker or len(fields) < 5 or not fs_fields:
            continue
        mount_point = fields[4].replace("\\040", " ")
        if target == mount_point or target.startswith(mount_point.rstrip("/") + "/"):
            candidate = (len(mount_point), fs_fields[0].casefold())
            if best is None or candidate[0] > best[0]:
                best = candidate
    return best[1] if best else None


@dataclass(frozen=True)
class ProductionPreflight:
    database_filesystem: str | None
    degraded: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return not self.degraded

    def summary(self) -> dict[str, object]:
        return {"status": "ok" if self.healthy else "degraded", "database_filesystem": self.database_filesystem or "unknown", "warnings": list(self.degraded)}


def run_production_preflight(config, *, adapter: OSAdapter | None = None) -> ProductionPreflight:
    if config.environment != "production":
        return ProductionPreflight(None)
    parsed = urlparse(config.public_url)
    if not parsed.scheme or not parsed.netloc or parsed.username or parsed.password:
        raise PreflightError("INKTIME_PUBLIC_URL 必須是沒有帳密的完整公開 URL")
    if parsed.scheme != "https" and not config.allow_insecure_http:
        raise PreflightError("Production HTTP 必須明確設定 INKTIME_ALLOW_INSECURE_HTTP=1")
    if parsed.scheme == "https" and not config.cookie_secure:
        raise PreflightError("Production HTTPS 必須設定 INKTIME_COOKIE_SECURE=1")
    if config.proxy_trust > 2:
        raise PreflightError("INKTIME_PROXY_TRUST 僅可設定 0 至 2 個受信任 proxy")
    fs_type = filesystem_for(config.database_path.parent, adapter)
    unsafe = fs_type in UNSAFE_NETWORK_FILESYSTEMS
    if unsafe and not config.allow_unsafe_network_database:
        raise PreflightError("Production SQLite、WAL 與鎖不得位於遠端網路掛載；僅能以 INKTIME_ALLOW_UNSAFE_NETWORK_DATABASE=1 明確覆寫")
    warnings: list[str] = []
    if unsafe:
        warnings.append("SQLite 位於不安全網路掛載；已由明確覆寫啟動，flock 與 WAL 未被停用")
    if parsed.scheme != "https":
        warnings.append("Production HTTP 已由明確覆寫啟動；Cookie 與傳輸安全降級")
    return ProductionPreflight(fs_type, tuple(warnings))
