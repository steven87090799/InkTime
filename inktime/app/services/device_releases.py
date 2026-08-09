from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
from typing import Any, Mapping

from inktime.app.core.paths import UnsafePathError
from inktime.app.db import Database
from inktime.app.domain.rendering import DeviceTestReleaseStore
from inktime.app.domain.rendering.release import (
    DEVICE_TEST_INDEX_DIRECTORY,
    ReleaseMetadataLockTimeout,
    STOCK_DIRECT_CLEANUP_STATE_DIRECTORY,
    STOCK_DIRECT_TEST_DEFERRED_DIRECTORY,
    STOCK_DIRECT_TEST_DIRECTORY,
    release_metadata_guard,
)


_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ACTIVE_QUEUE_STATES = ("READY", "AVAILABLE", "DOWNLOADED", "ACKNOWLEDGED")
_DOWNLOADABLE_RELEASE_STATES = {"published"}
_PAYLOAD_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_DEVICE_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_STOCK_CLEANUP_MARKERS = 32
MAX_STOCK_CLEANUP_PAYLOAD_BYTES = 8 * 1024 * 1024
STOCK_DIRECT_TEST_QUARANTINE_DIRECTORY = ".stock-direct-tests-quarantine"
_STOCK_MARKER_DIRECTORIES = (
    STOCK_DIRECT_TEST_DIRECTORY,
    STOCK_DIRECT_TEST_DEFERRED_DIRECTORY,
)
_LOGGER = logging.getLogger(__name__)


def payload_entry_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return the single safe firmware payload described by a release manifest."""
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("Release Manifest files 必須是陣列")
    candidates: list[dict[str, Any]] = []
    for raw_entry in files:
        if not isinstance(raw_entry, dict):
            raise ValueError("Release Manifest file entry 必須是物件")
        name = raw_entry.get("name")
        size = raw_entry.get("size")
        digest = raw_entry.get("sha256")
        if (
            not isinstance(name, str)
            or not name
            or name in {".", "..", "manifest.json"}
            or ".." in name
            or "\x00" in name
            or "/" in name
            or "\\" in name
        ):
            raise ValueError("Release Payload 檔名不合法")
        if not name.lower().endswith(".bin"):
            continue
        if type(size) is not int or not 1 <= size <= MAX_DEVICE_PAYLOAD_BYTES:
            raise ValueError("Release Payload size 不合法")
        if not isinstance(digest, str) or _PAYLOAD_SHA256.fullmatch(digest) is None:
            raise ValueError("Release Payload SHA-256 不合法")
        candidates.append({**raw_entry, "name": name, "size": size, "sha256": digest.lower()})
    if len(candidates) != 1:
        raise ValueError("Release Manifest 必須包含一個合法 .bin Payload")
    return candidates[0]


@dataclass(frozen=True)
class DeviceReleaseAuthorization:
    allowed: bool
    source: str | None
    reason: str | None
    release_id: str
    release_dir: Path | None = None
    manifest: dict[str, Any] | None = None
    test_assignment: dict[str, Any] | None = None
    release_dir_identity: tuple[int, int] | None = None


class DeviceReleaseService:
    def __init__(self, database: Database, release_root: Path) -> None:
        self.database = database
        self.release_root = release_root.resolve()
        self.test_store = DeviceTestReleaseStore(self.release_root)

    @staticmethod
    def _open_readonly(path: Path):
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise UnsafePathError("只允許讀取一般檔案")
        return os.fdopen(descriptor, "rb")

    @staticmethod
    @contextmanager
    def _open_directory(path: Path):
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise UnsafePathError("只允許讀取 Release 目錄")
            yield descriptor, (int(metadata.st_dev), int(metadata.st_ino))
        finally:
            os.close(descriptor)

    @staticmethod
    def _open_file_at(directory_fd: int, filename: str):
        if (
            not isinstance(filename, str)
            or not filename
            or filename in {".", ".."}
            or "\x00" in filename
            or "/" in filename
            or "\\" in filename
        ):
            raise UnsafePathError("Release 檔名不合法")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(filename, flags, dir_fd=directory_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise UnsafePathError("只允許讀取一般檔案")
        return os.fdopen(descriptor, "rb")

    @contextmanager
    def _open_release_directory(self, release_id: str):
        if _RELEASE_ID.fullmatch(release_id) is None:
            raise UnsafePathError("Release ID 不合法")
        with self._open_directory(self.release_root) as (root_fd, _root_identity):
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            release_fd = os.open(release_id, flags, dir_fd=root_fd)
            try:
                metadata = os.fstat(release_fd)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise UnsafePathError("Release 路徑不是目錄")
                yield release_fd, (int(metadata.st_dev), int(metadata.st_ino))
            finally:
                os.close(release_fd)

    def _pointer_release(self, profile_key: str) -> str | None:
        pointer = self.release_root / f"latest.{profile_key}"
        if not pointer.exists() and profile_key == "safe_4c":
            pointer = self.release_root / "latest"
        return self._read_pointer(pointer)

    def _read_pointer(self, pointer: Path) -> str | None:
        try:
            with self._open_readonly(pointer) as handle:
                value = handle.read(256).decode("utf-8").strip()
        except (FileNotFoundError, OSError, UnicodeDecodeError, UnsafePathError):
            return None
        return value if _RELEASE_ID.fullmatch(value) else None

    def _unlink_managed_file(
        self,
        directory_name: str,
        filename: str,
        *,
        expected: Mapping[str, Any] | None = None,
    ) -> None:
        if (
            not directory_name.startswith(".")
            or "/" in directory_name
            or "\\" in directory_name
            or not filename
            or "/" in filename
            or "\\" in filename
        ):
            return
        try:
            with self._open_directory(self.release_root / directory_name) as (
                directory_fd,
                _identity,
            ):
                metadata = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    return
                if expected is not None:
                    with self._open_file_at(directory_fd, filename) as handle:
                        raw = handle.read(64 * 1024 + 1)
                    if len(raw) > 64 * 1024:
                        return
                    current = json.loads(raw.decode("utf-8"))
                    if current != expected:
                        return
                os.unlink(filename, dir_fd=directory_fd)
        except (
            FileNotFoundError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            UnsafePathError,
        ):
            return

    def _load_manifest(
        self,
        release_id: str,
    ) -> tuple[Path, tuple[int, int], dict[str, Any]]:
        with self._open_release_directory(release_id) as (release_fd, identity):
            with self._open_file_at(release_fd, "manifest.json") as handle:
                raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Release Manifest 過大")
        manifest = json.loads(raw.decode("utf-8"))
        if not isinstance(manifest, dict) or str(manifest.get("release_id")) != release_id:
            raise ValueError("Release Manifest 身分不一致")
        return self.release_root / release_id, identity, manifest

    def _source(
        self,
        *,
        device_id: str,
        profile_key: str,
        release_id: str,
    ) -> tuple[str | None, dict[str, Any] | None]:
        assignment = self.test_store.active(device_id, profile_key)
        with self.database.session() as connection:
            formal = connection.execute(
                "SELECT 1 FROM device_render_releases WHERE device_id=? AND release_id=?",
                (device_id, release_id),
            ).fetchone()
            queue = connection.execute(
                """
                SELECT 1 FROM device_content_queue_items
                WHERE device_id=? AND release_id=?
                  AND status IN (?,?,?,?)
                  AND (expires_at IS NULL OR expires_at>?)
                """,
                (
                    device_id,
                    release_id,
                    *_ACTIVE_QUEUE_STATES,
                    datetime.now(timezone.utc).isoformat(),
                ),
            ).fetchone()
            release = connection.execute(
                "SELECT status FROM releases WHERE id=?",
                (release_id,),
            ).fetchone()
        release_status = str(release["status"]) if release is not None else None
        if assignment is not None and str(assignment.get("release_id")) == release_id:
            # Device test releases are intentionally filesystem-backed. If a
            # database row exists, however, it must still be downloadable.
            if release_status is not None and release_status not in _DOWNLOADABLE_RELEASE_STATES:
                return None, None
            return "test_assignment", assignment
        if release_status not in _DOWNLOADABLE_RELEASE_STATES:
            return None, None
        if formal is not None:
            return "device_assignment", None
        if self._pointer_release(profile_key) == release_id:
            return "profile_latest", None
        if queue is not None:
            return "queue", None
        return None, None

    def authorize_release_for_device(
        self,
        *,
        device_id: str,
        profile_key: str,
        release_id: str,
    ) -> DeviceReleaseAuthorization:
        if _RELEASE_ID.fullmatch(release_id) is None:
            return DeviceReleaseAuthorization(False, None, "invalid_release_id", release_id)
        source, assignment = self._source(
            device_id=device_id,
            profile_key=profile_key,
            release_id=release_id,
        )
        if source is None:
            return DeviceReleaseAuthorization(False, None, "not_assigned", release_id)
        try:
            release_dir, release_dir_identity, manifest = self._load_manifest(release_id)
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return DeviceReleaseAuthorization(False, None, "invalid_manifest", release_id)
        except UnsafePathError:
            return DeviceReleaseAuthorization(False, None, "unsafe_release_path", release_id)
        if str(manifest.get("render_profile")) != profile_key:
            return DeviceReleaseAuthorization(False, None, "profile_mismatch", release_id)
        return DeviceReleaseAuthorization(
            allowed=True,
            source=source,
            reason=None,
            release_id=release_id,
            release_dir=release_dir,
            manifest=manifest,
            test_assignment=assignment,
            release_dir_identity=release_dir_identity,
        )

    def authorize_stock_test_release_for_device(
        self,
        *,
        device_id: str,
        profile_key: str,
        release_id: str,
    ) -> DeviceReleaseAuthorization:
        """Authorize only an ephemeral Stock test release for one device.

        Stock test releases intentionally do not use the Custom firmware test
        assignment or queue.  Their manifest carries an exact device binding;
        this separate method keeps the generic release authorization contract
        unchanged.
        """
        if _RELEASE_ID.fullmatch(release_id) is None:
            return DeviceReleaseAuthorization(False, None, "invalid_release_id", release_id)
        with self.database.session() as connection:
            device = connection.execute(
                "SELECT enabled,delivery_mode,panel_profile FROM devices WHERE id=?",
                (device_id,),
            ).fetchone()
            release = connection.execute(
                "SELECT status FROM releases WHERE id=?",
                (release_id,),
            ).fetchone()
        if device is None:
            return DeviceReleaseAuthorization(False, None, "device_not_found", release_id)
        if not bool(device["enabled"]):
            return DeviceReleaseAuthorization(False, None, "device_disabled", release_id)
        if str(device["delivery_mode"] or "") != "stock_compat":
            return DeviceReleaseAuthorization(False, None, "not_stock_compatible", release_id)
        if str(device["panel_profile"] or "") != profile_key:
            return DeviceReleaseAuthorization(False, None, "profile_mismatch", release_id)
        if release is not None and str(release["status"]) not in _DOWNLOADABLE_RELEASE_STATES:
            return DeviceReleaseAuthorization(False, None, "release_not_downloadable", release_id)
        try:
            release_dir, release_dir_identity, manifest = self._load_manifest(release_id)
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return DeviceReleaseAuthorization(False, None, "invalid_manifest", release_id)
        except UnsafePathError:
            return DeviceReleaseAuthorization(False, None, "unsafe_release_path", release_id)
        options = manifest.get("render_options")
        if (
            manifest.get("release_kind") != "device_test"
            or str(manifest.get("render_profile")) != profile_key
            or not isinstance(options, dict)
            or options.get("transport") != "stock_direct"
            or options.get("stock_direct") is not True
            or str(options.get("stock_direct_device_id")) != device_id
        ):
            return DeviceReleaseAuthorization(False, None, "not_stock_test_release", release_id)
        authorization = DeviceReleaseAuthorization(
            allowed=True,
            source="stock_direct_test",
            reason=None,
            release_id=release_id,
            release_dir=release_dir,
            manifest=manifest,
            test_assignment=None,
            release_dir_identity=release_dir_identity,
        )
        try:
            entry = self.payload_entry_for_authorization(authorization)
            self.read_payload(authorization, str(entry["name"]))
        except (FileNotFoundError, OSError, PermissionError, UnsafePathError, ValueError):
            return DeviceReleaseAuthorization(False, None, "invalid_payload", release_id)
        return authorization

    @staticmethod
    def _stock_manifest_matches(
        manifest: Mapping[str, Any],
        *,
        device_id: str,
        profile_key: str,
        expires_at: str | None = None,
    ) -> bool:
        options = manifest.get("render_options")
        return bool(
            manifest.get("release_kind") == "device_test"
            and str(manifest.get("render_profile") or "") == profile_key
            and isinstance(options, dict)
            and options.get("transport") == "stock_direct"
            and options.get("stock_direct") is True
            and str(options.get("stock_direct_device_id") or "") == device_id
            and (expires_at is None or str(options.get("stock_direct_expires_at") or "") == expires_at)
        )

    def _stock_release_has_managed_reference(
        self,
        *,
        device_id: str,
        profile_key: str,
        release_id: str,
        custom_reference_ids: frozenset[str],
    ) -> bool:
        if release_id in custom_reference_ids:
            return True
        if any(
            self._read_pointer(pointer) == release_id
            for pointer in (
                self.release_root / "latest",
                self.release_root / f"latest.{profile_key}",
            )
        ):
            return True
        with self.database.session() as connection:
            referenced = connection.execute(
                """
                SELECT 1 FROM (
                    SELECT release_id AS value FROM device_render_releases WHERE release_id=?
                    UNION ALL
                    SELECT id AS value FROM releases WHERE id=?
                    UNION ALL
                    SELECT release_id AS value FROM device_content_queue_items WHERE release_id=?
                ) LIMIT 1
                """,
                (release_id, release_id, release_id),
            ).fetchone()
        return referenced is not None

    def _remove_stock_release(
        self,
        authorization: DeviceReleaseAuthorization,
        *,
        device_id: str,
        profile_key: str,
        expires_at: str | None = None,
        custom_reference_ids: frozenset[str],
    ) -> bool:
        if authorization.release_dir_identity is None:
            return False
        release_id = authorization.release_id
        try:
            release_dir, identity, manifest = self._load_manifest(release_id)
            if identity != authorization.release_dir_identity or not self._stock_manifest_matches(
                manifest,
                device_id=device_id,
                profile_key=profile_key,
                expires_at=expires_at,
            ):
                return False
            current = DeviceReleaseAuthorization(
                allowed=True,
                source=authorization.source,
                reason=None,
                release_id=release_id,
                release_dir=release_dir,
                manifest=manifest,
                release_dir_identity=identity,
            )
            entry = self.payload_entry_for_authorization(current)
            self.read_payload(current, str(entry["name"]))
        except (FileNotFoundError, OSError, PermissionError, UnsafePathError, ValueError):
            return False
        if self._stock_release_has_managed_reference(
            device_id=device_id,
            profile_key=profile_key,
            release_id=release_id,
            custom_reference_ids=custom_reference_ids,
        ):
            return False
        tombstone_name = f".stock-consumed-{release_id}-{secrets.token_hex(4)}"
        tombstone = self.release_root / tombstone_name
        try:
            with self._open_directory(self.release_root) as (root_fd, _root_identity):
                metadata = os.stat(release_id, dir_fd=root_fd, follow_symlinks=False)
                identity = (int(metadata.st_dev), int(metadata.st_ino))
                if not stat.S_ISDIR(metadata.st_mode) or identity != authorization.release_dir_identity:
                    raise UnsafePathError("Release 目錄在清理前已被替換")
                os.rename(
                    release_id,
                    tombstone_name,
                    src_dir_fd=root_fd,
                    dst_dir_fd=root_fd,
                )
        except (FileNotFoundError, OSError, UnsafePathError):
            return False
        options = manifest.get("render_options") or {}
        idempotency_key = str(options.get("idempotency_key") or "") if isinstance(options, dict) else ""
        if idempotency_key:
            digest = sha256(idempotency_key.encode("utf-8")).hexdigest()
            self._unlink_managed_file(
                DEVICE_TEST_INDEX_DIRECTORY,
                f"{digest}.json",
                expected={"release_id": release_id, "idempotency_key": idempotency_key},
            )
        for directory_name in _STOCK_MARKER_DIRECTORIES:
            self._unlink_managed_file(directory_name, f"{release_id}.json")
        shutil.rmtree(tombstone, ignore_errors=True)
        return True

    def consume_stock_test_release(
        self,
        *,
        device_id: str,
        profile_key: str,
        release_id: str,
    ) -> bool:
        """Revalidate and consume one unreferenced ephemeral Stock release."""
        try:
            with release_metadata_guard(self.release_root):
                authorization = self.authorize_stock_test_release_for_device(
                    device_id=device_id,
                    profile_key=profile_key,
                    release_id=release_id,
                )
                if not authorization.allowed or authorization.manifest is None:
                    return False
                with self.test_store.reference_snapshot() as (references, complete, _examined):
                    if not complete:
                        return False
                    return self._remove_stock_release(
                        authorization,
                        device_id=device_id,
                        profile_key=profile_key,
                        custom_reference_ids=references,
                    )
        except ReleaseMetadataLockTimeout:
            _LOGGER.warning("Deferred Stock release consumption because metadata lock timed out")
            return False
        except Exception:  # noqa: BLE001 -- post-upload cleanup must not trigger a duplicate send.
            _LOGGER.exception("Deferred Stock release consumption after an unexpected cleanup failure")
            return False

    def _cleanup_expired_stock_test_releases_locked(
        self,
        *,
        maximum: int = MAX_STOCK_CLEANUP_MARKERS,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Run one cleanup batch while the shared metadata guard is held."""
        limit = max(1, min(int(maximum), MAX_STOCK_CLEANUP_MARKERS))
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        examined = removed = validated_bytes = 0
        with self.test_store.reference_snapshot() as (
            custom_references,
            assignment_snapshot_complete,
            _assignment_examined,
        ):
            if not assignment_snapshot_complete:
                return {"examined": 0, "removed": 0}
            active = self._read_cleanup_active()
            for _pass_index in range(2):
                inactive = (
                    STOCK_DIRECT_TEST_DEFERRED_DIRECTORY
                    if active == STOCK_DIRECT_TEST_DIRECTORY
                    else STOCK_DIRECT_TEST_DIRECTORY
                )
                marker_root = self._marker_directory(active)
                if marker_root is None:
                    return {"examined": examined, "removed": removed}
                try:
                    entries = os.scandir(marker_root)
                except (FileNotFoundError, NotADirectoryError, OSError):
                    return {"examined": examined, "removed": removed}
                saw_entry = False
                with entries:
                    for entry in entries:
                        if examined >= limit:
                            break
                        saw_entry = True
                        examined += 1
                        if (
                            not entry.name.endswith(".json")
                            or _RELEASE_ID.fullmatch(entry.name[:-5]) is None
                            or entry.is_symlink()
                            or not entry.is_file(follow_symlinks=False)
                        ):
                            self._quarantine_marker(entry.name, active, "unsafe marker entry")
                            continue
                        retained = True
                        try:
                            with self._open_readonly(Path(entry.path)) as handle:
                                raw = handle.read(16 * 1024 + 1)
                            if len(raw) > 16 * 1024:
                                raise ValueError("Stock marker 過大")
                            marker = json.loads(raw.decode("utf-8"))
                            if not isinstance(marker, dict):
                                raise ValueError("Stock marker 格式不合法")
                            release_id = str(marker.get("release_id") or "")
                            device_id = str(marker.get("device_id") or "")
                            profile_key = str(marker.get("profile_key") or "")
                            expires_at = str(marker.get("expires_at") or "")
                            if release_id != entry.name[:-5]:
                                raise ValueError("Stock marker 身分不一致")
                            expiry = datetime.fromisoformat(expires_at)
                            if expiry.tzinfo is None:
                                raise ValueError("Stock marker 時區不合法")
                            if expiry <= current:
                                release_dir, identity, manifest = self._load_manifest(release_id)
                                if self._stock_manifest_matches(
                                    manifest,
                                    device_id=device_id,
                                    profile_key=profile_key,
                                    expires_at=expires_at,
                                ):
                                    authorization = DeviceReleaseAuthorization(
                                        allowed=True,
                                        source="stock_direct_test_expired",
                                        reason=None,
                                        release_id=release_id,
                                        release_dir=release_dir,
                                        manifest=manifest,
                                        release_dir_identity=identity,
                                    )
                                    payload_entry = self.payload_entry_for_authorization(authorization)
                                    payload_size = int(payload_entry["size"])
                                    if validated_bytes + payload_size <= MAX_STOCK_CLEANUP_PAYLOAD_BYTES:
                                        validated_bytes += payload_size
                                        if self._remove_stock_release(
                                            authorization,
                                            device_id=device_id,
                                            profile_key=profile_key,
                                            expires_at=expires_at,
                                            custom_reference_ids=custom_references,
                                        ):
                                            removed += 1
                                            retained = False
                        except (
                            FileNotFoundError,
                            OSError,
                            UnicodeDecodeError,
                            json.JSONDecodeError,
                            PermissionError,
                            UnsafePathError,
                            ValueError,
                        ) as exc:
                            self._quarantine_marker(entry.name, active, type(exc).__name__)
                            retained = False
                        if retained:
                            self._move_marker(entry.name, active, inactive)
                if examined >= limit or saw_entry:
                    break
                active = inactive
                if not self._write_cleanup_active(active):
                    break
        return {"examined": examined, "removed": removed}

    def cleanup_expired_stock_test_releases(
        self,
        *,
        maximum: int = MAX_STOCK_CLEANUP_MARKERS,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Bounded fair cleanup serialized with publishers in every process."""
        try:
            with release_metadata_guard(self.release_root):
                return self._cleanup_expired_stock_test_releases_locked(
                    maximum=maximum,
                    now=now,
                )
        except ReleaseMetadataLockTimeout:
            _LOGGER.warning("Deferred Stock cleanup because metadata lock timed out")
            return {"examined": 0, "removed": 0}
        except Exception:  # noqa: BLE001 -- opportunistic cleanup cannot block the primary request.
            _LOGGER.exception("Deferred Stock cleanup after an unexpected failure")
            return {"examined": 0, "removed": 0}

    def _cleanup_state_path(self) -> Path:
        return self.release_root / STOCK_DIRECT_CLEANUP_STATE_DIRECTORY / "state.json"

    def _write_cleanup_active(self, active: str) -> bool:
        if active not in _STOCK_MARKER_DIRECTORIES:
            return False
        root = self.release_root / STOCK_DIRECT_CLEANUP_STATE_DIRECTORY
        try:
            if root.is_symlink() or (root.exists() and not root.is_dir()):
                quarantined = self.release_root / (f".stock-cleanup-state-quarantine-{secrets.token_hex(6)}")
                os.rename(root, quarantined)
                _LOGGER.warning("Quarantined unsafe Stock cleanup state path")
            root.mkdir(mode=0o750, parents=True, exist_ok=True)
            if root.is_symlink() or root.resolve().parent != self.release_root:
                return False
            descriptor, temporary_name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=root)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump({"active": active}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, root / "state.json")
            finally:
                temporary.unlink(missing_ok=True)
        except OSError:
            return False
        return True

    def _read_cleanup_active(self) -> str:
        path = self._cleanup_state_path()
        try:
            if path.parent.is_symlink() or path.parent.resolve().parent != self.release_root:
                raise UnsafePathError("Stock cleanup state 路徑不安全")
            with self._open_readonly(path) as handle:
                raw = handle.read(4096 + 1)
            if len(raw) > 4096:
                raise ValueError("cleanup state oversized")
            state = json.loads(raw.decode("utf-8"))
            active = state.get("active") if isinstance(state, dict) else None
            if active in _STOCK_MARKER_DIRECTORIES:
                return str(active)
        except (
            FileNotFoundError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            UnsafePathError,
            ValueError,
        ):
            pass
        active = STOCK_DIRECT_TEST_DIRECTORY
        self._write_cleanup_active(active)
        return active

    def _marker_directory(self, directory_name: str) -> Path | None:
        if directory_name not in _STOCK_MARKER_DIRECTORIES:
            return None
        path = self.release_root / directory_name
        try:
            path.mkdir(mode=0o750, parents=True, exist_ok=True)
        except OSError:
            return None
        if path.is_symlink() or path.resolve().parent != self.release_root:
            return None
        return path

    def _move_marker(self, filename: str, source_name: str, destination_name: str) -> bool:
        source = self._marker_directory(source_name)
        destination = self._marker_directory(destination_name)
        if source is None or destination is None:
            return False
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            source_fd = os.open(source, flags)
            try:
                destination_fd = os.open(destination, flags)
                try:
                    os.link(
                        filename,
                        filename,
                        src_dir_fd=source_fd,
                        dst_dir_fd=destination_fd,
                        follow_symlinks=False,
                    )
                    os.unlink(filename, dir_fd=source_fd)
                finally:
                    os.close(destination_fd)
            finally:
                os.close(source_fd)
        except FileExistsError:
            try:
                source_path = source / filename
                destination_path = destination / filename
                with self._open_readonly(source_path) as source_handle:
                    source_raw = source_handle.read(16 * 1024 + 1)
                with self._open_readonly(destination_path) as destination_handle:
                    destination_raw = destination_handle.read(16 * 1024 + 1)
                if source_raw != destination_raw or len(source_raw) > 16 * 1024:
                    return False
                with self._open_directory(source) as (source_fd, _identity):
                    metadata = os.stat(filename, dir_fd=source_fd, follow_symlinks=False)
                    if stat.S_ISREG(metadata.st_mode):
                        os.unlink(filename, dir_fd=source_fd)
                return True
            except (FileNotFoundError, OSError, UnsafePathError):
                return False
        except OSError:
            return False
        return True

    def _quarantine_marker(self, filename: str, source_name: str, reason: str) -> bool:
        source = self._marker_directory(source_name)
        quarantine = self.release_root / STOCK_DIRECT_TEST_QUARANTINE_DIRECTORY
        if source is None:
            return False
        try:
            quarantine.mkdir(mode=0o750, parents=True, exist_ok=True)
            if quarantine.is_symlink() or quarantine.resolve().parent != self.release_root:
                return False
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            source_fd = os.open(source, flags)
            try:
                destination_fd = os.open(quarantine, flags)
                try:
                    destination = (
                        f"{filename}.{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
                        f".{secrets.token_hex(4)}.quarantine"
                    )
                    os.rename(
                        filename,
                        destination,
                        src_dir_fd=source_fd,
                        dst_dir_fd=destination_fd,
                    )
                finally:
                    os.close(destination_fd)
            finally:
                os.close(source_fd)
        except OSError:
            return False
        _LOGGER.warning("Quarantined invalid Stock cleanup marker %s: %s", filename, reason)
        return True

    def latest_for_device(
        self,
        *,
        device_id: str,
        profile_key: str,
    ) -> DeviceReleaseAuthorization:
        assignment = self.test_store.active(device_id, profile_key)
        if assignment is not None:
            candidate = str(assignment.get("release_id", ""))
        else:
            with self.database.session() as connection:
                formal = connection.execute(
                    "SELECT release_id FROM device_render_releases WHERE device_id=?",
                    (device_id,),
                ).fetchone()
            candidate = (
                str(formal["release_id"])
                if formal is not None
                else str(self._pointer_release(profile_key) or "")
            )
        return self.authorize_release_for_device(
            device_id=device_id,
            profile_key=profile_key,
            release_id=candidate,
        )

    def read_payload(
        self,
        authorization: DeviceReleaseAuthorization,
        filename: str,
    ) -> tuple[bytes, dict[str, Any]]:
        if (
            not authorization.allowed
            or authorization.release_dir is None
            or authorization.release_dir_identity is None
        ):
            raise PermissionError("Release 未授權")
        entry = self.payload_entry_for_authorization(authorization)
        if filename != entry["name"]:
            raise FileNotFoundError(filename)
        try:
            with self._open_release_directory(authorization.release_id) as (
                release_fd,
                identity,
            ):
                if identity != authorization.release_dir_identity:
                    raise UnsafePathError("Release 目錄在授權後已被替換")
                with self._open_file_at(release_fd, filename) as handle:
                    payload = handle.read()
        except UnsafePathError:
            raise
        except OSError as exc:
            raise FileNotFoundError(filename) from exc
        expected_size = entry["size"]
        if expected_size != len(payload) or sha256(payload).hexdigest() != str(entry["sha256"]):
            raise ValueError("Release Payload 完整性驗證失敗")
        return payload, entry

    def payload_entry_for_authorization(
        self,
        authorization: DeviceReleaseAuthorization,
    ) -> dict[str, Any]:
        """Validate directory identity and payload metadata without pathname reads."""
        if not authorization.allowed or authorization.release_dir_identity is None:
            raise PermissionError("Release 未授權")
        with self._open_release_directory(authorization.release_id) as (_release_fd, identity):
            if identity != authorization.release_dir_identity:
                raise UnsafePathError("Release 目錄在授權後已被替換")
        return payload_entry_from_manifest(authorization.manifest or {})
