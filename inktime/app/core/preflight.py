"""Fail-closed checks for the small set of production deployment boundaries.

The inspector intentionally does not try to make SQLite safe on network storage:
WAL and flock are retained, but a remote mount is refused before either is used.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
from typing import Mapping, Protocol
from urllib.parse import urlparse


UNSAFE_NETWORK_FILESYSTEMS = frozenset(
    {"nfs", "nfs4", "cifs", "smbfs", "sshfs", "fuse.sshfs", "fuseblk", "9p"}
)


class PreflightError(ValueError):
    """Deployment configuration must be corrected before startup."""

    def __init__(self, code: str, message: str, fix: str) -> None:
        self.code = code
        self.message = message
        self.fix = fix
        super().__init__(f"{code} {message}；修正方式：{fix}")


def _fail(code: str, message: str, fix: str) -> None:
    raise PreflightError(code, message, fix)


class OSAdapter(Protocol):
    def mountinfo(self) -> str: ...


class NativeOSAdapter:
    def mountinfo(self) -> str:
        try:
            return Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except OSError:
            return ""


def _trusted_lan_hostname(hostname: str, *, allow_test_host: bool = False) -> bool:
    normalized = hostname.rstrip(".").casefold()
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        if normalized.endswith(".local") and len(normalized) > len(".local"):
            return True
        if allow_test_host and normalized == "inktime-lan.test":
            return True
        return "." not in normalized and normalized not in {"example", "invalid"}
    return address.is_private or address.is_loopback or address.is_link_local


def validate_public_url(config, *, mode: str | None = None, allow_test_host: bool = False):
    parsed = urlparse(config.public_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        _fail(
            "PREFLIGHT-URL-001",
            "INKTIME_PUBLIC_URL 必須是 http 或 https 的完整 Origin，且不可包含帳密、路徑、Query 或 Fragment",
            "改用例如 https://inktime.example.net 或 http://192.168.1.100:8765",
        )
    if parsed.scheme == "http":
        if config.cookie_secure:
            _fail(
                "PREFLIGHT-HTTP-001",
                "INKTIME_PUBLIC_URL 使用 HTTP 時必須設定 INKTIME_COOKIE_SECURE=0，否則瀏覽器不會送回 Session Cookie",
                "在 LAN 專用 env 設定 INKTIME_COOKIE_SECURE=0；公開部署請改用 HTTPS",
            )
        if not config.allow_insecure_http:
            _fail(
                "PREFLIGHT-HTTP-002",
                "INKTIME_PUBLIC_URL 使用 HTTP 時必須明確 opt-in",
                "可信任 LAN 設定 INKTIME_ALLOW_INSECURE_HTTP=1；公開部署請改用 HTTPS",
            )
    if config.environment == "production":
        hostname = (parsed.hostname or "").rstrip(".").casefold()
        resolved_mode = mode or ("lan" if parsed.scheme == "http" else "https")
        if resolved_mode == "lan":
            if parsed.scheme != "http":
                _fail(
                    "PREFLIGHT-LAN-001",
                    "LAN Production 必須使用明確 HTTP Origin",
                    "將 INKTIME_PUBLIC_URL 設為可信任 LAN 的 http:// 位址",
                )
            if config.proxy_trust != 0:
                _fail(
                    "PREFLIGHT-LAN-002",
                    "LAN Production 不得信任未配置的 Reverse Proxy",
                    "設定 INKTIME_PROXY_TRUST=0",
                )
            if not _trusted_lan_hostname(hostname, allow_test_host=allow_test_host):
                _fail(
                    "PREFLIGHT-LAN-003",
                    "LAN HTTP Host 不是 RFC1918、loopback、link-local、.local 或單標籤內網 hostname",
                    "使用實際內網 IP、inktime.local 或明確單標籤 hostname；公網 hostname 必須改用 HTTPS",
                )
            return parsed
        reserved_suffixes = (".example", ".invalid", ".localhost", ".test")
        placeholder = (
            hostname
            in {
                "localhost",
                "127.0.0.1",
                "::1",
                "example.com",
                "example.net",
                "example.org",
            }
            or hostname.endswith(reserved_suffixes)
            or hostname.endswith((".example.com", ".example.net", ".example.org"))
        )
        if placeholder:
            _fail(
                "PREFLIGHT-HTTPS-001",
                "Production HTTPS 的 INKTIME_PUBLIC_URL 不可使用 localhost 或範例網域",
                "改成實際 HTTPS Origin",
            )
        if parsed.scheme == "https" and not config.cookie_secure:
            _fail(
                "PREFLIGHT-HTTPS-002",
                "Production HTTPS 必須設定 INKTIME_COOKIE_SECURE=1",
                "設定 INKTIME_COOKIE_SECURE=1",
            )
    return parsed


def validate_lan_environment(environ: Mapping[str, str], compose_text: str) -> None:
    """Validate host-side LAN deployment values without exposing their contents."""

    exact = {
        "INKTIME_ENVIRONMENT": "production",
        "INKTIME_ALLOW_INSECURE_HTTP": "1",
        "INKTIME_COOKIE_SECURE": "0",
        "INKTIME_PROXY_TRUST": "0",
        "INKTIME_ALLOW_UNSAFE_NETWORK_DATABASE": "0",
    }
    for name, expected in exact.items():
        if str(environ.get(name, "")).strip() != expected:
            _fail(
                "PREFLIGHT-LAN-ENV-001",
                f"{name} 必須明確設定為 {expected}",
                "使用 .env.lan.production.example 建立專用 LAN Production env",
            )
    for name in ("INKTIME_DATA_PATH", "INKTIME_PHOTO_PATH"):
        raw = str(environ.get(name, "")).strip()
        if not raw or "change_me" in raw.casefold() or not Path(raw).expanduser().is_absolute():
            _fail(
                "PREFLIGHT-LAN-PATH-001",
                f"{name} 必須是已替換 placeholder 的絕對路徑",
                "改成 NAS/host 上實際存在且權限正確的絕對路徑",
            )
        if "simulation_photos" in raw.casefold():
            _fail(
                "PREFLIGHT-LAN-PATH-002",
                f"{name} 不得使用 simulation_photos",
                "改成正式資料或唯讀照片目錄",
            )
    data_path = Path(str(environ["INKTIME_DATA_PATH"])).expanduser().resolve()
    photo_path = Path(str(environ["INKTIME_PHOTO_PATH"])).expanduser().resolve()
    if _paths_overlap(data_path, photo_path):
        _fail(
            "PREFLIGHT-LAN-PATH-003",
            "Data path 與 Photo path 不得相同或互為父子路徑",
            "使用獨立可寫資料目錄與唯讀照片目錄",
        )
    image_tag = str(environ.get("INKTIME_IMAGE_TAG", "")).strip()
    revision = str(environ.get("INKTIME_GIT_REVISION", "")).strip()
    if image_tag.casefold() in {"", "local", "unknown", "change_me_git_sha"}:
        _fail(
            "PREFLIGHT-LAN-BUILD-001",
            "正式 LAN image tag 不可為 local、unknown 或 placeholder",
            "使用 scripts/build_release_image.sh 產生 immutable Git SHA tag",
        )
    if revision.casefold() in {"", "unknown", "change_me_git_sha"}:
        _fail(
            "PREFLIGHT-LAN-BUILD-002",
            "正式 LAN build 必須提供 Git revision",
            "將 INKTIME_GIT_REVISION 設為實際完整 Git SHA",
        )
    compact_compose = compose_text.replace(" ", "")
    if "target:/photos" not in compact_compose or "read_only:true" not in compact_compose:
        _fail(
            "PREFLIGHT-LAN-MOUNT-001",
            "Compose 的 /photos mount 必須唯讀",
            "將照片 long bind mount 設為 target: /photos 與 read_only: true",
        )


@dataclass(frozen=True)
class MountInfo:
    filesystem: str
    mount_point: str
    read_only: bool


def _decode_mountinfo_path(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _mountinfo_entries(adapter: OSAdapter | None = None) -> tuple[MountInfo, ...]:
    text = (adapter or NativeOSAdapter()).mountinfo()
    entries: list[MountInfo] = []
    for line in text.splitlines():
        before, marker, after = line.partition(" - ")
        fields, fs_fields = before.split(), after.split()
        if not marker or len(fields) < 6 or not fs_fields:
            continue
        mount_options = {item.casefold() for item in fields[5].split(",")}
        entries.append(
            MountInfo(
                fs_fields[0].casefold(),
                _decode_mountinfo_path(fields[4]),
                "ro" in mount_options,
            )
        )
    return tuple(entries)


def mount_for(path: Path, adapter: OSAdapter | None = None) -> MountInfo | None:
    """Return the deepest component-aware Linux mount and its effective mode.

    macOS does not expose Linux mountinfo.  Tests can provide an adapter and
    production macOS deployments should use a local APFS data directory.
    """
    target = str(path.expanduser().resolve())
    best: tuple[int, MountInfo] | None = None
    for entry in _mountinfo_entries(adapter):
        mount_point = entry.mount_point
        if target == mount_point or target.startswith(mount_point.rstrip("/") + "/"):
            candidate = (len(mount_point), entry)
            if best is None or candidate[0] > best[0]:
                best = candidate
    return best[1] if best else None


def mounts_at_or_below(path: Path, adapter: OSAdapter | None = None) -> tuple[MountInfo, ...]:
    """Return exact and descendant mounts using path-component boundaries."""

    target = str(path.expanduser().resolve()).rstrip("/") or "/"
    prefix = target.rstrip("/") + "/"
    return tuple(
        entry
        for entry in _mountinfo_entries(adapter)
        if entry.mount_point == target or entry.mount_point.startswith(prefix)
    )


def filesystem_for(path: Path, adapter: OSAdapter | None = None) -> str | None:
    mount = mount_for(path, adapter)
    return mount.filesystem if mount else None


def _paths_overlap(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class ProductionPreflight:
    database_filesystem: str | None
    transport: str = "https"
    security_state: str = "secure"
    tls_enabled: bool = True
    secure_cookie: bool = True
    degraded: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return not self.degraded

    def summary(self) -> dict[str, object]:
        return {
            "status": "ok" if self.healthy else "degraded",
            "transport": self.transport,
            "security_state": self.security_state,
            "tls_enabled": self.tls_enabled,
            "secure_cookie": self.secure_cookie,
            "database_filesystem": self.database_filesystem or "unknown",
            "warnings": list(self.degraded),
        }


def run_production_preflight(
    config,
    *,
    adapter: OSAdapter | None = None,
    mode: str | None = None,
    allow_test_host: bool | None = None,
) -> ProductionPreflight:
    allow_test = (
        os.environ.get("INKTIME_LAN_TEST_MODE", "0") == "1" if allow_test_host is None else allow_test_host
    )
    parsed = validate_public_url(config, mode=mode, allow_test_host=allow_test)
    if config.environment != "production":
        return ProductionPreflight(
            None,
            "development-http",
            "development",
            False,
            config.cookie_secure,
        )
    if config.proxy_trust > 2:
        _fail(
            "PREFLIGHT-PROXY-001",
            "INKTIME_PROXY_TRUST 僅可設定 0 至 2 個受信任 proxy",
            "設定實際 proxy hop 數；LAN 直連必須為 0",
        )
    if config.database_path.parent != config.data_dir:
        _fail(
            "PREFLIGHT-DB-001",
            "Production SQLite database 必須直接位於 /data 對應的 data directory",
            "設定 INKTIME_DATABASE=/data/inktime.db",
        )
    if _paths_overlap(config.photo_dir, config.data_dir):
        _fail(
            "DEPLOY-PATH-OVERLAP-001",
            "Production photo 與 data directory 不得相同或互為父子路徑",
            "使用彼此獨立的 /data 可寫 bind mount 與 /photos 唯讀 bind mount",
        )
    photo_target = str(config.photo_dir.expanduser().resolve())
    photo_mounts = mounts_at_or_below(config.photo_dir, adapter)
    exact_photo_mounts = tuple(
        entry for entry in photo_mounts if entry.mount_point == photo_target
    )
    if not exact_photo_mounts or any(not entry.read_only for entry in exact_photo_mounts):
        _fail(
            "DEPLOY-PHOTO-RO-001",
            "Production /photos 必須是作業系統實際回報的精確唯讀 mount",
            "以 Compose long bind syntax 設定獨立的 /photos mount、read_only: true，並確認 mountinfo 精確顯示 ro",
        )
    writable_nested = next(
        (
            entry
            for entry in photo_mounts
            if entry.mount_point != photo_target and not entry.read_only
        ),
        None,
    )
    if writable_nested is not None:
        _fail(
            "DEPLOY-PHOTO-RO-002",
            "Production /photos 含有可寫入的下層 mount",
            "將照片樹下每一個 nested mount 全部改為唯讀，或改用不含可寫下層 mount 的照片根目錄",
        )
    fs_type = filesystem_for(config.database_path.parent, adapter)
    unsafe = fs_type in UNSAFE_NETWORK_FILESYSTEMS
    if (unsafe or fs_type is None) and not config.allow_unsafe_network_database:
        _fail(
            "PREFLIGHT-DB-002",
            "Production SQLite、WAL 與鎖不得位於未明確允許的遠端網路掛載",
            "把 /data 放在本機 filesystem；僅在已接受風險時設定 INKTIME_ALLOW_UNSAFE_NETWORK_DATABASE=1",
        )
    warnings: list[str] = []
    if unsafe or fs_type is None:
        warnings.append("SQLite 位於不安全網路掛載；已由明確覆寫啟動，flock 與 WAL 未被停用")
    if parsed.scheme != "https":
        warnings.append("Production HTTP 已由明確覆寫啟動；Cookie 與傳輸安全降級")
    lan_http = parsed.scheme == "http"
    return ProductionPreflight(
        fs_type,
        "trusted-lan-http" if lan_http else "https",
        "degraded" if lan_http else "secure",
        not lan_http,
        config.cookie_secure,
        tuple(warnings),
    )
