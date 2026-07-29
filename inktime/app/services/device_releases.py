from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from inktime.app.core.paths import UnsafePathError, safe_join
from inktime.app.db import Database
from inktime.app.domain.rendering import DeviceTestReleaseStore


_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ACTIVE_QUEUE_STATES = ("READY", "AVAILABLE", "DOWNLOADED", "ACKNOWLEDGED")
_INVALID_RELEASE_STATES = {"staged_failed", "orphaned", "deleted", "withdrawn"}


@dataclass(frozen=True)
class DeviceReleaseAuthorization:
    allowed: bool
    source: str | None
    reason: str | None
    release_id: str
    release_dir: Path | None = None
    manifest: dict[str, Any] | None = None
    test_assignment: dict[str, Any] | None = None


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

    def _load_manifest(self, release_id: str) -> tuple[Path, dict[str, Any]]:
        release_dir = safe_join(self.release_root, release_id)
        if release_dir.parent != self.release_root:
            raise UnsafePathError("Release 路徑不合法")
        manifest_path = safe_join(release_dir, "manifest.json")
        with self._open_readonly(manifest_path) as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Release Manifest 過大")
        manifest = json.loads(raw.decode("utf-8"))
        if not isinstance(manifest, dict) or str(manifest.get("release_id")) != release_id:
            raise ValueError("Release Manifest 身分不一致")
        return release_dir, manifest

    def _source(
        self,
        *,
        device_id: str,
        profile_key: str,
        release_id: str,
    ) -> tuple[str | None, dict[str, Any] | None]:
        assignment = self.test_store.active(device_id, profile_key)
        if assignment is not None and str(assignment.get("release_id")) == release_id:
            return "test_assignment", assignment
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
        if release is not None and str(release["status"]) in _INVALID_RELEASE_STATES:
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
            release_dir, manifest = self._load_manifest(release_id)
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return DeviceReleaseAuthorization(False, None, "invalid_manifest", release_id)
        except UnsafePathError:
            return DeviceReleaseAuthorization(False, None, "unsafe_release_path", release_id)
        if str(manifest.get("render_profile")) != profile_key:
            return DeviceReleaseAuthorization(False, None, "profile_mismatch", release_id)
        return DeviceReleaseAuthorization(
            True,
            source,
            None,
            release_id,
            release_dir,
            manifest,
            assignment,
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
        if not authorization.allowed or authorization.release_dir is None:
            raise PermissionError("Release 未授權")
        manifest = authorization.manifest or {}
        entry = next(
            (
                item
                for item in manifest.get("files", [])
                if isinstance(item, dict) and str(item.get("name")) == filename
            ),
            None,
        )
        if entry is None or filename == "manifest.json":
            raise FileNotFoundError(filename)
        path = safe_join(authorization.release_dir, filename)
        try:
            with self._open_readonly(path) as handle:
                payload = handle.read()
        except OSError as exc:
            raise FileNotFoundError(filename) from exc
        expected_size = entry.get("size")
        if (
            type(expected_size) is not int
            or expected_size != len(payload)
            or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", "")))
            or sha256(payload).hexdigest() != str(entry["sha256"])
        ):
            raise ValueError("Release Payload 完整性驗證失敗")
        return payload, entry
