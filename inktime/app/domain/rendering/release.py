from __future__ import annotations

from builtins import list as builtin_list
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import re
import stat
import tempfile
from threading import RLock, local
import time
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - production containers are Linux/POSIX.
    fcntl = None  # type: ignore[assignment]

from PIL import Image

from .palette import DisplayProfile, encode_image, get_display_profile


FOUR_COLORS = ((0, 0, 0), (255, 255, 255), (220, 30, 30), (245, 190, 25))
STOCK_DIRECT_TEST_TTL_SECONDS = 45 * 60
DEVICE_TEST_INDEX_DIRECTORY = ".device-test-index"
DEVICE_TEST_INDEX_MIGRATION_DIRECTORY = ".device-test-index-migration"
DEVICE_TEST_INDEX_QUARANTINE_DIRECTORY = ".device-test-index-quarantine"
DEVICE_TEST_INDEX_MIGRATION_VERSION = 1
STOCK_DIRECT_TEST_DIRECTORY = ".stock-direct-tests"
STOCK_DIRECT_TEST_DEFERRED_DIRECTORY = ".stock-direct-tests-deferred"
STOCK_DIRECT_CLEANUP_STATE_DIRECTORY = ".stock-direct-cleanup-state"
DEVICE_TEST_ASSIGNMENT_QUARANTINE_DIRECTORY = ".device-tests-quarantine"
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ASSIGNMENT_LOCK = RLock()
_RELEASE_METADATA_LOCK = RLock()
_RELEASE_METADATA_LOCAL = local()
RELEASE_METADATA_LOCK_TIMEOUT_SECONDS = 5.0
RELEASE_METADATA_LOCK_POLL_SECONDS = 0.05
_LOGGER = logging.getLogger(__name__)


class ReleaseMetadataLockTimeout(RuntimeError):
    """The shared Release metadata transaction could not start in time."""


def fsync_directory(path: Path) -> None:
    """Persist directory entry updates where the host filesystem supports it."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


@contextmanager
def release_metadata_guard(
    root: Path,
    *,
    timeout: float = RELEASE_METADATA_LOCK_TIMEOUT_SECONDS,
):
    """Serialize short Release metadata transactions across processes.

    Linux production uses an advisory ``flock`` whose ownership is released by
    the OS if a process exits.  The process-local RLock makes the guard safely
    reentrant for helper calls in the same thread.  Non-POSIX development hosts
    retain only the explicitly weaker process-local guarantee.
    """

    resolved = root.resolve()
    key = str(resolved)
    with _RELEASE_METADATA_LOCK:
        held = getattr(_RELEASE_METADATA_LOCAL, "held", None)
        if held is None:
            held = {}
            _RELEASE_METADATA_LOCAL.held = held
        current = held.get(key)
        if current is not None:
            descriptor, depth = current
            held[key] = (descriptor, depth + 1)
            try:
                yield
            finally:
                descriptor, depth = held[key]
                held[key] = (descriptor, depth - 1)
            return

        descriptor = -1
        try:
            if fcntl is not None:
                flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(resolved / ".release-metadata.lock", flags, 0o600)
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ValueError("RENDER-010 Release metadata lock 不是一般檔案")
                os.fchmod(descriptor, 0o600)
                deadline = time.monotonic() + max(0.0, float(timeout))
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError as exc:
                        if time.monotonic() >= deadline:
                            raise ReleaseMetadataLockTimeout("RENDER-011 Release metadata lock 逾時") from exc
                        time.sleep(
                            min(RELEASE_METADATA_LOCK_POLL_SECONDS, max(0.0, deadline - time.monotonic()))
                        )
            held[key] = (descriptor, 1)
            yield
        finally:
            held.pop(key, None)
            if descriptor >= 0:
                try:
                    if fcntl is not None:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)


def _nearest_color(pixel) -> int:
    return min(
        range(4),
        key=lambda index: sum(
            (int(pixel[channel]) - FOUR_COLORS[index][channel]) ** 2 for channel in range(3)
        ),
    )


def pack_four_color_2bpp(image: Image.Image) -> bytes:
    return encode_image(
        image,
        profile_key="safe_4c",
        dither="none",
        color_distance="rgb",
        strength=0,
    ).payload


class AtomicReleasePublisher:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def publish(
        self,
        images: list[tuple[str, Image.Image]],
        *,
        profile_key: str = "safe_4c",
        dither: str = "floyd_steinberg",
        color_distance: str = "oklab",
        dither_strength: float = 1.0,
        width: int = 480,
        height: int = 800,
        orientation: str = "portrait",
        profile_override: DisplayProfile | None = None,
        linear_light: bool = False,
        protected_mask: Image.Image | None = None,
        activate: bool = True,
        release_kind: str = "formal",
        metadata: dict | None = None,
    ) -> dict:
        if not images:
            raise ValueError("RENDER-001 至少需要一張圖片")
        if orientation not in {"portrait", "landscape"}:
            raise ValueError("RENDER-005 不支援的相框方向")
        profile = profile_override or get_display_profile(profile_key)
        if profile.key != profile_key:
            raise ValueError("RENDER-006 自訂色盤與面板 Profile 不一致")
        if release_kind not in {"formal", "device_test"}:
            raise ValueError("RENDER-008 Release 類型不合法")
        release_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        temporary = self.root / f".{release_id}.tmp"
        final = self.root / release_id
        temporary.mkdir(mode=0o750)
        effective_strength = 1.0 if dither in {"gooddisplay", "photo_smooth"} else float(dither_strength)
        effective_color_distance = "rgb" if dither in {"gooddisplay", "photo_smooth"} else color_distance
        files = []
        output_palette = profile.colors
        try:
            for index, (photo_id, source) in enumerate(images, 1):
                rendered = source.convert("RGB")
                if rendered.size != (width, height):
                    raise ValueError(f"RENDER-002 圖片尺寸必須是 {width}×{height}")
                encoded = encode_image(
                    rendered,
                    profile_key=profile_key,
                    dither=dither,
                    color_distance=effective_color_distance,
                    strength=effective_strength,
                    linear_light=linear_light,
                    protected_mask=protected_mask,
                    profile=profile,
                )
                payload = encoded.payload
                output_palette = encoded.palette
                expected = width * height // (4 if profile.pixel_format == "2bpp" else 2)
                if len(payload) != expected:
                    raise ValueError("RENDER-002 索引影像檔案大小驗證失敗")
                filename = f"photo_{index}.bin"
                preview = f"preview_{index}.png"
                (temporary / filename).write_bytes(payload)
                encoded.preview.save(temporary / preview, "PNG")
                files.append(
                    {
                        "name": filename,
                        "size": len(payload),
                        "sha256": sha256(payload).hexdigest(),
                        "source_photo_id": photo_id,
                        "preview": preview,
                    }
                )
            manifest: dict[str, Any] = {
                "schema_version": 1 if profile.pixel_format == "2bpp" else 2,
                "release_id": release_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "display_type": profile.display_type,
                "render_profile": profile.key,
                "panel_profile": profile.panel_profile,
                "palette_version": profile.palette_version,
                "release_kind": release_kind,
                "width": width,
                "height": height,
                "pixel_format": profile.pixel_format,
                "orientation": orientation,
                "panel_capabilities": {
                    "supports_partial_refresh": profile.supports_partial_refresh,
                    "requires_full_refresh": profile.requires_full_refresh,
                    "supports_hibernate": profile.supports_hibernate,
                    "minimum_refresh_interval_seconds": profile.minimum_refresh_interval_seconds,
                },
                "dither": dither,
                "dither_strength": effective_strength,
                "color_distance": effective_color_distance,
                "palette": [
                    {"code": color.code, "name": color.name, "rgb": list(color.rgb)}
                    for color in output_palette
                ],
                "files": files,
            }
            if metadata:
                manifest["render_options"] = metadata
                # Additive metadata: device payload readers continue to use files.
                if "photo_orientations" in metadata:
                    manifest["photo_orientations"] = metadata["photo_orientations"]
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            (temporary / "manifest.json").write_bytes(manifest_bytes)
            for path in temporary.iterdir():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            with release_metadata_guard(self.root):
                temporary.replace(final)
                fsync_directory(self.root)
                if activate:
                    pointer_tmp = self.root / ".latest.tmp"
                    pointer_tmp.write_text(release_id, encoding="utf-8")
                    with pointer_tmp.open("rb") as stream:
                        os.fsync(stream.fileno())
                    pointer_tmp.replace(self.root / "latest")
                    fsync_directory(self.root)
                    profile_pointer_tmp = self.root / f".latest.{profile.key}.tmp"
                    profile_pointer_tmp.write_text(release_id, encoding="utf-8")
                    with profile_pointer_tmp.open("rb") as stream:
                        os.fsync(stream.fileno())
                    profile_pointer_tmp.replace(self.root / f"latest.{profile.key}")
                    fsync_directory(self.root)
            return manifest
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise

    def list(self) -> list[dict]:
        releases = []
        for manifest_path in self.root.glob("*/manifest.json"):
            try:
                if manifest_path.parent.is_symlink() or manifest_path.is_symlink():
                    continue
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(manifest, dict)
                    or str(manifest.get("release_id") or "") != manifest_path.parent.name
                    or not isinstance(manifest.get("created_at"), str)
                ):
                    continue
                releases.append(manifest)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
        return sorted(releases, key=lambda item: item["created_at"], reverse=True)

    def _device_test_index_path(self, idempotency_key: str) -> Path:
        digest = sha256(idempotency_key.encode("utf-8")).hexdigest()
        return self.root / DEVICE_TEST_INDEX_DIRECTORY / f"{digest}.json"

    @staticmethod
    def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_mode),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
        )

    def _observe_device_test_index(
        self, path: Path
    ) -> tuple[dict[str, Any] | None, tuple[int, int, int, int, int] | None, str]:
        """Read one index without following links and retain replacement identity."""

        parent = path.parent
        if parent.is_symlink() or (parent.exists() and parent.resolve().parent != self.root):
            return None, None, "unsafe_directory"
        try:
            metadata = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            return None, None, "missing"
        identity = self._metadata_identity(metadata)
        if not stat.S_ISREG(metadata.st_mode):
            return None, identity, "unsafe_file_type"
        if metadata.st_size > 64 * 1024:
            return None, identity, "oversized"
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if self._metadata_identity(opened) != identity:
                return None, identity, "replaced_during_read"
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(64 * 1024 + 1)
        finally:
            os.close(descriptor)
        if len(raw) > 64 * 1024:
            return None, identity, "oversized"
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, identity, "malformed_json"
        if not isinstance(parsed, dict):
            return None, identity, "invalid_contract"
        return parsed, identity, "valid_json"

    def _quarantine_observed_index(
        self,
        path: Path,
        identity: tuple[int, int, int, int, int] | None,
        reason: str,
    ) -> bool:
        """Atomically preserve an invalid index only if it is the observed object."""

        if identity is None or path.parent != self.root / DEVICE_TEST_INDEX_DIRECTORY:
            return False
        quarantine = self.root / DEVICE_TEST_INDEX_QUARANTINE_DIRECTORY
        try:
            if path.parent.is_symlink() or path.parent.resolve().parent != self.root:
                return False
            quarantine.mkdir(mode=0o750, parents=True, exist_ok=True)
            if quarantine.is_symlink() or quarantine.resolve().parent != self.root:
                return False
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            source_fd = os.open(path.parent, flags)
            try:
                current = os.stat(path.name, dir_fd=source_fd, follow_symlinks=False)
                if self._metadata_identity(current) != identity:
                    return False
                destination_fd = os.open(quarantine, flags)
                try:
                    destination = (
                        f"{path.stem}.{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
                        f".{secrets.token_hex(4)}.quarantine"
                    )
                    os.rename(
                        path.name,
                        destination,
                        src_dir_fd=source_fd,
                        dst_dir_fd=destination_fd,
                    )
                finally:
                    os.close(destination_fd)
            finally:
                os.close(source_fd)
        except (FileNotFoundError, OSError):
            return False
        _LOGGER.warning("Quarantined invalid device-test index %s: %s", path.name, reason)
        return True

    def _unlink_managed_index(
        self,
        directory_name: str,
        filename: str,
        *,
        expected: dict[str, Any] | None = None,
    ) -> None:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.root / directory_name, flags)
            try:
                metadata = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    return
                if expected is not None:
                    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                    file_descriptor = os.open(filename, flags, dir_fd=descriptor)
                    with os.fdopen(file_descriptor, "rb") as handle:
                        raw = handle.read(64 * 1024 + 1)
                    if len(raw) > 64 * 1024:
                        return
                    try:
                        current = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        return
                    if current != expected:
                        return
                os.unlink(filename, dir_fd=descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            return

    def _atomic_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if path.parent.is_symlink() or path.parent.resolve().parent != self.root:
            raise ValueError("RENDER-010 Release 索引路徑不安全")
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _atomic_json_create(self, path: Path, payload: dict[str, Any]) -> bool:
        """Atomically create JSON without replacing another operation's state."""
        path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if path.parent.is_symlink() or path.parent.resolve().parent != self.root:
            raise ValueError("RENDER-010 Release 索引路徑不安全")
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                return False
            return True
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_regular_json(path: Path, *, maximum_bytes: int = 64 * 1024) -> dict[str, Any]:
        if path.is_symlink():
            raise ValueError("RENDER-010 JSON metadata 路徑不安全")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise ValueError("RENDER-010 JSON metadata 不是一般檔案")
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read(maximum_bytes + 1)
        if len(raw) > maximum_bytes:
            raise ValueError("RENDER-010 JSON metadata 過大")
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("RENDER-010 JSON metadata 格式不合法")
        return parsed

    def _stock_marker_directory_for_write(self) -> str:
        state_path = self.root / STOCK_DIRECT_CLEANUP_STATE_DIRECTORY / "state.json"
        active = STOCK_DIRECT_TEST_DIRECTORY
        try:
            if state_path.parent.is_symlink() or state_path.parent.resolve().parent != self.root:
                raise ValueError("RENDER-010 Stock cleanup state 路徑不安全")
            state = self._read_regular_json(state_path, maximum_bytes=4096)
            candidate = str(state.get("active") or "")
            if candidate in {STOCK_DIRECT_TEST_DIRECTORY, STOCK_DIRECT_TEST_DEFERRED_DIRECTORY}:
                active = candidate
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            pass
        return (
            STOCK_DIRECT_TEST_DEFERRED_DIRECTORY
            if active == STOCK_DIRECT_TEST_DIRECTORY
            else STOCK_DIRECT_TEST_DIRECTORY
        )

    @staticmethod
    def _device_test_index_payload(manifest: dict[str, Any]) -> dict[str, str] | None:
        options = manifest.get("render_options")
        if manifest.get("release_kind") != "device_test" or not isinstance(options, dict):
            return None
        idempotency_key = options.get("idempotency_key")
        transport = options.get("transport")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            return None
        if transport not in {"custom", "stock_direct"}:
            return None
        if transport == "stock_direct" and (
            options.get("stock_direct") is not True
            or not isinstance(options.get("stock_direct_device_id"), str)
            or not options.get("stock_direct_device_id")
        ):
            return None
        if transport == "custom" and options.get("stock_direct") is True:
            return None
        release_id = manifest.get("release_id")
        profile_key = manifest.get("render_profile")
        if (
            not isinstance(release_id, str)
            or _RELEASE_ID.fullmatch(release_id) is None
            or not isinstance(profile_key, str)
            or not profile_key
        ):
            return None
        return {"release_id": release_id, "idempotency_key": idempotency_key}

    @staticmethod
    def _legacy_candidate_order(manifest: dict[str, Any]) -> tuple[datetime, str] | None:
        created_at = manifest.get("created_at")
        release_id = manifest.get("release_id")
        if not isinstance(created_at, str) or not isinstance(release_id, str):
            return None
        try:
            created = datetime.fromisoformat(created_at)
        except ValueError:
            return None
        if created.tzinfo is None:
            return None
        return created.astimezone(timezone.utc), release_id

    def _validated_index_manifest(
        self,
        index_path: Path,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        indexed, identity, observation = self._observe_device_test_index(index_path)
        if observation == "missing":
            return None
        if indexed is None:
            self._quarantine_observed_index(index_path, identity, observation)
            return None
        if (
            str(indexed.get("idempotency_key") or "") != idempotency_key
            or not isinstance(indexed.get("release_id"), str)
            or _RELEASE_ID.fullmatch(str(indexed["release_id"])) is None
        ):
            self._quarantine_observed_index(index_path, identity, "identity_mismatch")
            return None
        try:
            manifest = self.validate(str(indexed["release_id"]))
        except (FileNotFoundError, NotADirectoryError, ValueError):
            self._quarantine_observed_index(index_path, identity, "invalid_target")
            return None
        except OSError:
            # A transient storage failure is not proof that the indexed target
            # is stale. Preserve the exact observed index and fail this lookup
            # closed so a later retry cannot create a duplicate Release.
            raise
        if (
            self._device_test_index_payload(manifest) != indexed
            or self._legacy_candidate_order(manifest) is None
        ):
            self._quarantine_observed_index(index_path, identity, "target_contract_mismatch")
            return None
        return manifest

    def _device_test_index_migration_complete(self) -> bool:
        state_path = self.root / DEVICE_TEST_INDEX_MIGRATION_DIRECTORY / "state.json"
        if state_path.parent.is_symlink() or (
            state_path.parent.exists() and state_path.parent.resolve().parent != self.root
        ):
            raise ValueError("RENDER-010 index migration state 路徑不安全")
        try:
            state = self._read_regular_json(state_path, maximum_bytes=4096)
        except (FileNotFoundError, NotADirectoryError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        except ValueError:
            return False
        return bool(
            state.get("schema_version") == 1
            and state.get("migration_version") == DEVICE_TEST_INDEX_MIGRATION_VERSION
            and state.get("complete") is True
            and isinstance(state.get("completed_at"), str)
        )

    def _backfill_legacy_device_test_indexes(self) -> None:
        """Stream the legacy store once per version with deterministic winners."""
        state_path = self.root / DEVICE_TEST_INDEX_MIGRATION_DIRECTORY / "state.json"
        if self._device_test_index_migration_complete():
            return
        with os.scandir(self.root) as entries:
            for entry in entries:
                if (
                    _RELEASE_ID.fullmatch(entry.name) is None
                    or entry.is_symlink()
                    or not entry.is_dir(follow_symlinks=False)
                ):
                    continue
                try:
                    manifest = self.validate(entry.name)
                    payload = self._device_test_index_payload(manifest)
                    candidate_order = self._legacy_candidate_order(manifest)
                except (FileNotFoundError, NotADirectoryError, ValueError):
                    continue
                if payload is None or candidate_order is None:
                    continue
                index_path = self._device_test_index_path(payload["idempotency_key"])
                current = self._validated_index_manifest(index_path, payload["idempotency_key"])
                current_order = self._legacy_candidate_order(current) if current else None
                if current_order is not None and current_order >= candidate_order:
                    continue
                if not self._atomic_json_create(index_path, payload):
                    self._atomic_json(index_path, payload)
        state_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if state_path.parent.is_symlink() or state_path.parent.resolve().parent != self.root:
            raise ValueError("RENDER-010 index migration state 路徑不安全")
        self._atomic_json(
            state_path,
            {
                "schema_version": 1,
                "migration_version": DEVICE_TEST_INDEX_MIGRATION_VERSION,
                "complete": True,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _write_device_test_indexes(self, manifest: dict[str, Any]) -> None:
        options = manifest.get("render_options") or {}
        if not isinstance(options, dict):
            return
        idempotency_key = str(options.get("idempotency_key") or "")
        if not idempotency_key:
            return
        index_path = self._device_test_index_path(idempotency_key)
        index_payload = {
            "release_id": manifest["release_id"],
            "idempotency_key": idempotency_key,
        }
        created: list[tuple[str, str, dict[str, Any]]] = []
        try:
            if self._atomic_json_create(index_path, index_payload):
                created.append((DEVICE_TEST_INDEX_DIRECTORY, index_path.name, index_payload))
            else:
                existing = self._validated_index_manifest(index_path, idempotency_key)
                if existing is None:
                    if not self._atomic_json_create(index_path, index_payload):
                        raise FileExistsError("RENDER-010 idempotency index 正在被更新")
                    created.append((DEVICE_TEST_INDEX_DIRECTORY, index_path.name, index_payload))
                elif self._device_test_index_payload(existing) != index_payload:
                    raise FileExistsError("RENDER-010 idempotency index 已由其他 Release 使用")
            if options.get("stock_direct") is not True:
                return
            marker_directory = self._stock_marker_directory_for_write()
            marker_root = self.root / marker_directory
            marker = marker_root / f"{manifest['release_id']}.json"
            marker_payload = {
                "release_id": manifest["release_id"],
                "device_id": str(options.get("stock_direct_device_id") or ""),
                "profile_key": str(manifest.get("render_profile") or ""),
                "expires_at": str(options.get("stock_direct_expires_at") or ""),
            }
            if self._atomic_json_create(marker, marker_payload):
                created.append((marker_directory, marker.name, marker_payload))
            elif self._read_regular_json(marker) != marker_payload:
                raise FileExistsError("RENDER-010 Stock marker 已存在")
        except Exception:
            for directory_name, filename, payload in reversed(created):
                self._unlink_managed_index(directory_name, filename, expected=payload)
            raise

    def find_device_test_by_idempotency(self, idempotency_key: str) -> dict | None:
        with release_metadata_guard(self.root):
            return self._find_device_test_by_idempotency(idempotency_key)

    def _find_device_test_by_idempotency(self, idempotency_key: str) -> dict | None:
        index_path = self._device_test_index_path(idempotency_key)
        try:
            manifest = self._validated_index_manifest(index_path, idempotency_key)
            if manifest is not None:
                return manifest
            self._backfill_legacy_device_test_indexes()
            return self._validated_index_manifest(index_path, idempotency_key)
        except (OSError, ValueError, ReleaseMetadataLockTimeout):
            return None

    def discard_unassigned_device_test(self, release_id: str, idempotency_key: str) -> None:
        with release_metadata_guard(self.root):
            release = self.root / release_id
            try:
                manifest = self.validate(release_id)
            except (OSError, ValueError):
                return
            options = manifest.get("render_options") or {}
            if (
                release.parent == self.root
                and manifest.get("release_kind") == "device_test"
                and isinstance(options, dict)
                and str(options.get("idempotency_key") or "") == idempotency_key
            ):
                shutil.rmtree(release, ignore_errors=True)
                digest = sha256(idempotency_key.encode("utf-8")).hexdigest()
                self._unlink_managed_index(
                    DEVICE_TEST_INDEX_DIRECTORY,
                    f"{digest}.json",
                    expected={"release_id": release_id, "idempotency_key": idempotency_key},
                )
                for directory_name in (
                    STOCK_DIRECT_TEST_DIRECTORY,
                    STOCK_DIRECT_TEST_DEFERRED_DIRECTORY,
                ):
                    self._unlink_managed_index(directory_name, f"{release_id}.json")

    def publish_preencoded(
        self,
        *,
        source_photo_id: str,
        payload_path: Path,
        preview_path: Path,
        profile_key: str,
        dither: str,
        color_distance: str,
        dither_strength: float,
        linear_light: bool,
        palette: builtin_list[dict[str, Any]],
        palette_version: str,
        metadata: dict[str, Any],
    ) -> dict:
        """Commit verified child output without re-running Pillow/NumPy rendering."""

        profile = get_display_profile(profile_key)
        metadata = dict(metadata)
        if metadata.get("stock_direct") is True:
            metadata["stock_direct_expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=STOCK_DIRECT_TEST_TTL_SECONDS)
            ).isoformat()
        payload = payload_path.read_bytes()
        expected = 480 * 800 // (4 if profile.pixel_format == "2bpp" else 2)
        if len(payload) != expected:
            raise ValueError("RENDER-002 索引影像檔案大小驗證失敗")
        with Image.open(preview_path) as opened:
            opened.verify()
            if opened.size != (480, 800):
                raise ValueError("RENDER-002 Preview 尺寸不合法")
        release_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        temporary = self.root / f".{release_id}.tmp"
        final = self.root / release_id
        temporary.mkdir(mode=0o750)
        try:
            payload_target = temporary / "photo_1.bin"
            preview_target = temporary / "preview_1.png"
            shutil.copyfile(payload_path, payload_target)
            shutil.copyfile(preview_path, preview_target)
            manifest: dict[str, Any] = {
                "schema_version": 1 if profile.pixel_format == "2bpp" else 2,
                "release_id": release_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "display_type": profile.display_type,
                "render_profile": profile.key,
                "panel_profile": profile.panel_profile,
                "palette_version": str(palette_version)[:80],
                "release_kind": "device_test",
                "width": 480,
                "height": 800,
                "pixel_format": profile.pixel_format,
                "orientation": "portrait",
                "panel_capabilities": {
                    "supports_partial_refresh": profile.supports_partial_refresh,
                    "requires_full_refresh": profile.requires_full_refresh,
                    "supports_hibernate": profile.supports_hibernate,
                    "minimum_refresh_interval_seconds": profile.minimum_refresh_interval_seconds,
                },
                "dither": dither,
                "dither_strength": float(dither_strength),
                "color_distance": color_distance,
                "palette": palette,
                "files": [
                    {
                        "name": "photo_1.bin",
                        "size": len(payload),
                        "sha256": sha256(payload).hexdigest(),
                        "source_photo_id": source_photo_id,
                        "preview": "preview_1.png",
                    }
                ],
                "render_options": dict(metadata, linear_light=bool(linear_light)),
            }
            (temporary / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            for path in temporary.iterdir():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            with release_metadata_guard(self.root):
                temporary.replace(final)
                try:
                    self._write_device_test_indexes(manifest)
                except Exception:
                    shutil.rmtree(final, ignore_errors=True)
                    raise
            return manifest
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            if final.exists():
                shutil.rmtree(final, ignore_errors=True)
            raise

    def validate(self, release_id: str) -> dict:
        if _RELEASE_ID.fullmatch(release_id) is None:
            raise ValueError("RENDER-010 Release ID 不合法")
        release_dir = self.root / release_id
        manifest_path = release_dir / "manifest.json"
        if (
            release_dir.parent != self.root
            or release_dir.is_symlink()
            or manifest_path.is_symlink()
            or not manifest_path.is_file()
        ):
            raise ValueError("RENDER-010 Release Manifest 不存在")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("RENDER-010 Release Manifest 無法解析") from exc
        if not isinstance(manifest, dict):
            raise ValueError("RENDER-010 Release Manifest 格式不合法")
        if str(manifest.get("release_id")) != release_id:
            raise ValueError("RENDER-010 Release ID 與 Manifest 不一致")
        get_display_profile(str(manifest.get("render_profile", "")))
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("RENDER-010 Release 沒有 Payload")
        for entry in files:
            if not isinstance(entry, dict):
                raise ValueError("RENDER-010 Release 檔案描述不合法")
            name = str(entry.get("name", ""))
            path = release_dir / name
            size = entry.get("size")
            digest = entry.get("sha256")
            if (
                not name
                or name in {".", ".."}
                or "\x00" in name
                or "/" in name
                or "\\" in name
                or path.parent != release_dir
                or path.is_symlink()
                or not path.is_file()
                or type(size) is not int
                or size < 0
                or not isinstance(digest, str)
            ):
                raise ValueError("RENDER-010 Release Payload 不存在")
            payload = path.read_bytes()
            if len(payload) != size:
                raise ValueError("RENDER-010 Release Payload 大小不一致")
            if sha256(payload).hexdigest() != digest:
                raise ValueError("RENDER-010 Release Payload SHA-256 不一致")
        return manifest

    def pointer_snapshot(self, profile_keys: builtin_list[str]) -> dict[str, str | None]:
        names = [f"latest.{key}" for key in dict.fromkeys(profile_keys)] + ["latest"]
        snapshot: dict[str, str | None] = {}
        for name in names:
            path = self.root / name
            try:
                snapshot[name] = path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                snapshot[name] = None
        return snapshot

    def restore_pointers(self, snapshot: dict[str, str | None]) -> None:
        with release_metadata_guard(self.root):
            for name, release_id in snapshot.items():
                path = self.root / name
                if release_id is None:
                    path.unlink(missing_ok=True)
                    continue
                temporary = self.root / f".{name}.restore.tmp"
                temporary.write_text(release_id, encoding="utf-8")
                temporary.replace(path)

    def activate_manifests(self, manifests: builtin_list[dict]) -> None:
        if not manifests:
            raise ValueError("RENDER-010 沒有可啟用的 Release")
        with release_metadata_guard(self.root):
            for manifest in manifests:
                release_id = str(manifest["release_id"])
                profile_key = str(manifest["render_profile"])
                self.validate(release_id)
                temporary = self.root / f".latest.{profile_key}.tmp"
                temporary.write_text(release_id, encoding="utf-8")
                temporary.replace(self.root / f"latest.{profile_key}")
            # 保留舊版只讀取 latest 的相容契約；以第一個 Profile 為正式預設。
            release_id = str(manifests[0]["release_id"])
            temporary = self.root / ".latest.tmp"
            temporary.write_text(release_id, encoding="utf-8")
            temporary.replace(self.root / "latest")

    def mark_orphan(self, release_id: str, reason: str) -> None:
        with release_metadata_guard(self.root):
            release_dir = self.root / release_id
            if release_dir.parent != self.root or not release_dir.is_dir():
                return
            state = {
                "status": "orphan",
                "reason": reason[:500],
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            temporary = release_dir / ".inktime-state.tmp"
            temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            temporary.replace(release_dir / ".inktime-state.json")

    def delete_release(self, release_id: str) -> bool:
        """Remove one formal Release directory after the DB has fenced it."""

        if _RELEASE_ID.fullmatch(str(release_id)) is None:
            return False
        with release_metadata_guard(self.root):
            release_dir = self.root / str(release_id)
            if release_dir.parent != self.root or release_dir.is_symlink() or not release_dir.is_dir():
                return False
            shutil.rmtree(release_dir)
            fsync_directory(self.root)
            return True

    def rollback(self, release_id: str) -> None:
        with release_metadata_guard(self.root):
            target = self.root / release_id / "manifest.json"
            if not target.is_file() or target.parent.parent != self.root:
                raise KeyError(release_id)
            temporary = self.root / ".latest.tmp"
            temporary.write_text(release_id, encoding="utf-8")
            temporary.replace(self.root / "latest")
            manifest = json.loads(target.read_text(encoding="utf-8"))
            profile_key = str(manifest.get("render_profile", "safe_4c"))
            get_display_profile(profile_key)
            profile_temporary = self.root / f".latest.{profile_key}.tmp"
            profile_temporary.write_text(release_id, encoding="utf-8")
            profile_temporary.replace(self.root / f"latest.{profile_key}")


class DeviceTestReleaseStore:
    """One request-local test release assignment per device, outside formal pointers."""

    _DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

    def __init__(self, release_root: Path) -> None:
        self.release_root = release_root.resolve()
        self.root = self.release_root / ".device-tests"
        self.release_root.mkdir(mode=0o750, parents=True, exist_ok=True)
        with release_metadata_guard(self.release_root):
            self.root.mkdir(mode=0o750, parents=True, exist_ok=True)
            if self.root.is_symlink() or self.root.resolve().parent != self.release_root:
                raise ValueError("DEVICE-006 Custom assignment store 路徑不安全")

    def _path(self, device_id: str) -> Path:
        if not self._DEVICE_ID.fullmatch(device_id):
            raise ValueError("DEVICE-006 裝置識別碼不合法")
        if self.root.is_symlink() or self.root.resolve().parent != self.release_root:
            raise ValueError("DEVICE-006 Custom assignment store 路徑不安全")
        return self.root / f"{device_id}.json"

    def assign(
        self,
        device_id: str,
        release_id: str,
        *,
        profile_key: str,
        delivery: str,
        one_time: bool,
        restore_formal: bool,
    ) -> dict:
        if delivery not in {"immediate", "next_wake"}:
            raise ValueError("DEVICE-006 測試傳送時機不合法")
        assignment = {
            "device_id": device_id,
            "release_id": release_id,
            "profile_key": profile_key,
            "delivery": delivery,
            "one_time": bool(one_time),
            "restore_formal": bool(restore_formal),
            "status": "assigned",
            "assigned_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": datetime.now(timezone.utc).timestamp() + 86400,
            "retry_count": 0,
        }
        path = self._path(device_id)
        temporary = path.with_suffix(".tmp")
        with release_metadata_guard(self.release_root), _ASSIGNMENT_LOCK:
            manifest_path = self.release_root / release_id / "manifest.json"
            if (
                not manifest_path.is_file()
                or manifest_path.is_symlink()
                or manifest_path.parent.is_symlink()
                or manifest_path.parent.parent != self.release_root
            ):
                raise ValueError("DEVICE-006 測試 Release 不存在")
            temporary.write_text(json.dumps(assignment, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        return assignment

    def active(self, device_id: str, profile_key: str) -> dict | None:
        with release_metadata_guard(self.release_root), _ASSIGNMENT_LOCK:
            path = self._path(device_id)
            try:
                assignment = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                return None
            if not isinstance(assignment, dict):
                return None
            allowed = {
                "assigned",
                "manifest_fetched",
                "payload_downloaded",
                "payload_verified",
                "display_confirmed",
            }
            if assignment.get("status") not in allowed or assignment.get("profile_key") != profile_key:
                return None
            try:
                expired = (
                    float(assignment.get("expires_at", 0)) <= datetime.now(timezone.utc).timestamp()
                    or int(assignment.get("retry_count", 0)) >= 5
                )
            except (TypeError, ValueError):
                return None
            if expired:
                assignment["status"] = "expired"
                assignment["expired_at"] = datetime.now(timezone.utc).isoformat()
                self._write(path, assignment)
                return None
            manifest_path = self.release_root / str(assignment.get("release_id", "")) / "manifest.json"
            if not manifest_path.is_file() or manifest_path.parent.parent != self.release_root:
                return None
            if assignment.get("status") == "assigned":
                assignment["status"] = "manifest_fetched"
                assignment["manifest_fetched_at"] = datetime.now(timezone.utc).isoformat()
                self._write(path, assignment)
            return assignment

    def references_release(self, device_id: str, release_id: str) -> bool:
        """Check one exact device assignment without advancing its lifecycle."""
        with release_metadata_guard(self.release_root), _ASSIGNMENT_LOCK:
            path = self._path(device_id)
            try:
                assignment = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                return False
            return isinstance(assignment, dict) and str(assignment.get("release_id") or "") == release_id

    def _quarantine_assignment(self, entry_name: str, reason: str) -> bool:
        quarantine = self.release_root / DEVICE_TEST_ASSIGNMENT_QUARANTINE_DIRECTORY
        try:
            quarantine.mkdir(mode=0o750, parents=True, exist_ok=True)
            if quarantine.is_symlink() or quarantine.resolve().parent != self.release_root:
                return False
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            source_fd = os.open(self.root, directory_flags)
            try:
                destination_fd = os.open(quarantine, directory_flags)
                try:
                    destination = (
                        f"{entry_name[:160]}.{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
                        f".{secrets.token_hex(4)}.quarantine"
                    )
                    os.rename(
                        entry_name,
                        destination,
                        src_dir_fd=source_fd,
                        dst_dir_fd=destination_fd,
                    )
                finally:
                    os.close(destination_fd)
            finally:
                os.close(source_fd)
        except OSError:
            _LOGGER.error("Unable to quarantine Custom assignment %s: %s", entry_name, reason)
            return False
        _LOGGER.warning("Quarantined invalid Custom assignment %s: %s", entry_name, reason)
        return True

    def _reference_snapshot_locked(self, *, maximum: int) -> tuple[frozenset[str], bool, int]:
        if self.root.is_symlink() or self.root.resolve().parent != self.release_root:
            return frozenset(), False, 0
        limit = max(1, int(maximum))
        referenced: set[str] = set()
        examined = 0
        try:
            entries = os.scandir(self.root)
        except OSError:
            return frozenset(), False, 0
        with entries:
            for entry in entries:
                if entry.name.endswith(".tmp") or not entry.name.endswith(".json"):
                    continue
                if examined >= limit:
                    _LOGGER.error("Custom assignment snapshot exceeded the %d-entry safety limit", limit)
                    return frozenset(), False, examined
                examined += 1
                match = re.fullmatch(r"([A-Za-z0-9_-]{1,100})\.json", entry.name)
                if match is None:
                    if not self._quarantine_assignment(entry.name, "non-canonical filename"):
                        return frozenset(), False, examined
                    continue
                device_id = match.group(1)
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    if not self._quarantine_assignment(entry.name, "unsafe file type"):
                        return frozenset(), False, examined
                    continue
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                try:
                    descriptor = os.open(entry.path, flags)
                    with os.fdopen(descriptor, "rb") as handle:
                        raw = handle.read(64 * 1024 + 1)
                    if len(raw) > 64 * 1024:
                        raise ValueError("oversized assignment")
                    assignment = json.loads(raw.decode("utf-8"))
                    release_id = assignment.get("release_id") if isinstance(assignment, dict) else None
                    if (
                        not isinstance(assignment, dict)
                        or assignment.get("device_id") != device_id
                        or not isinstance(release_id, str)
                        or _RELEASE_ID.fullmatch(release_id) is None
                    ):
                        raise ValueError("invalid assignment contract")
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    if not self._quarantine_assignment(entry.name, type(exc).__name__):
                        return frozenset(), False, examined
                    continue
                referenced.add(release_id)
        return frozenset(referenced), True, examined

    @contextmanager
    def reference_snapshot(self, *, maximum: int = 1024):
        """Yield one bounded snapshot while blocking every compliant writer."""
        with release_metadata_guard(self.release_root), _ASSIGNMENT_LOCK:
            yield self._reference_snapshot_locked(maximum=maximum)

    def references_release_any(self, release_id: str, *, maximum: int = 1024) -> bool:
        """Compatibility wrapper; cleanup batches use one shared snapshot instead."""
        with self.reference_snapshot(maximum=maximum) as (referenced, complete, _examined):
            return not complete or release_id in referenced

    def mark_downloaded(self, device_id: str, release_id: str) -> None:
        with release_metadata_guard(self.release_root), _ASSIGNMENT_LOCK:
            path = self._path(device_id)
            try:
                assignment = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                return
            if not isinstance(assignment, dict):
                return
            if assignment.get("release_id") != release_id or assignment.get("status") not in {
                "manifest_fetched",
                "payload_downloaded",
            }:
                return
            assignment["status"] = "payload_downloaded"
            assignment["payload_downloaded_at"] = datetime.now(timezone.utc).isoformat()
            assignment["retry_count"] = int(assignment.get("retry_count", 0)) + 1
            self._write(path, assignment)

    def confirm_display(
        self,
        device_id: str,
        release_id: str,
        *,
        profile_key: str,
        payload_verified: bool,
        display_updated: bool,
        error_code: str,
    ) -> bool:
        with release_metadata_guard(self.release_root), _ASSIGNMENT_LOCK:
            path = self._path(device_id)
            try:
                assignment = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                return False
            if not isinstance(assignment, dict):
                return False
            if (
                assignment.get("release_id") != release_id
                or assignment.get("profile_key") != profile_key
                or assignment.get("status") not in {"payload_downloaded", "payload_verified"}
                or not payload_verified
                or not display_updated
                or bool(error_code)
            ):
                return False
            assignment["status"] = "payload_verified"
            assignment["payload_verified_at"] = datetime.now(timezone.utc).isoformat()
            assignment["status"] = "display_confirmed"
            assignment["display_confirmed_at"] = datetime.now(timezone.utc).isoformat()
            if assignment.get("one_time") or assignment.get("restore_formal"):
                assignment["status"] = "consumed"
                assignment["consumed_at"] = datetime.now(timezone.utc).isoformat()
            self._write(path, assignment)
            return True

    @staticmethod
    def _write(path: Path, assignment: dict) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(assignment, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
