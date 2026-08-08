from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

from inktime.app.core.paths import UnsafePathError
from inktime.app.db import Database
from inktime.app.domain.rendering import DeviceTestReleaseStore


_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ACTIVE_QUEUE_STATES = ("READY", "AVAILABLE", "DOWNLOADED", "ACKNOWLEDGED")
_DOWNLOADABLE_RELEASE_STATES = {"published"}
_PAYLOAD_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_DEVICE_PAYLOAD_BYTES = 64 * 1024 * 1024


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
        try:
            with self._open_readonly(pointer) as handle:
                value = handle.read(256).decode("utf-8").strip()
        except (FileNotFoundError, OSError, UnicodeDecodeError, UnsafePathError):
            return None
        return value if _RELEASE_ID.fullmatch(value) else None

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
        return DeviceReleaseAuthorization(
            allowed=True,
            source="stock_direct_test",
            reason=None,
            release_id=release_id,
            release_dir=release_dir,
            manifest=manifest,
            test_assignment=None,
            release_dir_identity=release_dir_identity,
        )

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
