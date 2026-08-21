#!/usr/bin/env python3
"""Create a verified NAS recovery point without writing the live data root."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile

# ``python scripts/create_update_recovery.py`` sets ``sys.path[0]`` to
# ``/app/scripts`` inside the runtime image. Add the application root so the
# direct updater invocation can import the installed-source namespace package.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inktime.app.core.preflight import OSAdapter, mounts_at_or_below
from inktime.app.db import Database
from inktime.app.services.backups import BackupService


CONTRACT_FILE = Path("/app/nas-deployment-contract.version")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _digest(path: Path) -> str:
    result = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _require_regular_file(path: Path, code: str, description: str) -> os.stat_result:
    try:
        details = os.lstat(path)
    except OSError as exc:
        raise RuntimeError(f"{code} {description} 必須是既有的一般檔案") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise RuntimeError(f"{code} {description} 必須是一般檔案且不可為 symlink")
    return details


def _copy_session_key(source: Path, destination: Path) -> str:
    details = _require_regular_file(source, "NAS-RECOVERY-SESSION-001", "/source/session.key")
    if stat.S_IMODE(details.st_mode) != 0o600:
        raise RuntimeError("NAS-RECOVERY-SESSION-002 /source/session.key 權限必須為 0600")
    handle = tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=".session-key-",
        delete=False,
    )
    staged = Path(handle.name)
    try:
        with source.open("rb") as reader, handle:
            shutil.copyfileobj(reader, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, 0o600)
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    return _digest(destination)


def _require_mount(
    path: Path,
    *,
    read_only: bool,
    adapter: OSAdapter | None,
) -> None:
    expected = str(path.resolve())
    exact_mounts = tuple(
        mount
        for mount in mounts_at_or_below(path, adapter)
        if mount.mount_point == expected
    )
    if not exact_mounts or any(mount.read_only is not read_only for mount in exact_mounts):
        if read_only:
            raise RuntimeError(
                "NAS-RECOVERY-SOURCE-RO-001 recovery source 必須是精確的唯讀 mount"
            )
        raise RuntimeError(
            "NAS-RECOVERY-DEST-RW-001 recovery destination 必須是精確的可寫 mount"
        )


def _verify_destination_writable(destination: Path) -> None:
    handle = None
    probe: Path | None = None
    try:
        handle = tempfile.NamedTemporaryFile(
            dir=destination,
            prefix=".recovery-write-probe-",
            delete=False,
        )
        probe = Path(handle.name)
        handle.write(b"bounded recovery write probe\n")
        handle.flush()
        os.fsync(handle.fileno())
    except OSError as exc:
        raise RuntimeError(
            "NAS-RECOVERY-DEST-RW-001 recovery destination 無法安全寫入"
        ) from exc
    finally:
        if handle is not None:
            handle.close()
        if probe is not None:
            probe.unlink(missing_ok=True)


def _snapshot_read_only_database(source: Path, destination: Path) -> None:
    source_connection: sqlite3.Connection | None = None
    target_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
        source_connection.execute("PRAGMA query_only = ON")
        query_only = source_connection.execute("PRAGMA query_only").fetchone()
        if query_only is None or int(query_only[0]) != 1:
            raise RuntimeError("NAS-RECOVERY-DB-001 production SQLite query_only 驗證失敗")
        target_connection = sqlite3.connect(destination)
        source_connection.backup(target_connection)
        integrity = target_connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise RuntimeError("NAS-RECOVERY-DB-001 recovery SQLite snapshot 完整性檢查失敗")
        target_connection.commit()
    except sqlite3.Error as exc:
        raise RuntimeError("NAS-RECOVERY-DB-001 無法建立唯讀 production SQLite snapshot") from exc
    finally:
        if target_connection is not None:
            target_connection.close()
        if source_connection is not None:
            source_connection.close()
    with destination.open("rb") as stream:
        os.fsync(stream.fileno())


def create_recovery(
    *,
    source_root: Path,
    destination_root: Path,
    previous_image_ref: str,
    previous_image_digest: str,
    target_image_ref: str,
    deployment_contract: str,
    image_contract: str,
    adapter: OSAdapter | None = None,
) -> Path:
    source_input = source_root.expanduser()
    destination_input = destination_root.expanduser()
    if source_input.is_symlink():
        raise RuntimeError("NAS-RECOVERY-SOURCE-RO-001 recovery source root 無效")
    if destination_input.is_symlink():
        raise RuntimeError("NAS-RECOVERY-DEST-RW-001 recovery destination root 無效")
    source_root = source_input.resolve()
    destination_root = destination_input.resolve()
    if not source_root.is_dir():
        raise RuntimeError("NAS-RECOVERY-SOURCE-RO-001 recovery source root 無效")
    if not destination_root.is_dir():
        raise RuntimeError("NAS-RECOVERY-DEST-RW-001 recovery destination root 無效")
    if source_root == destination_root:
        raise RuntimeError("NAS-RECOVERY-DEST-RW-001 recovery source 與 destination 不得相同")
    _require_mount(source_root, read_only=True, adapter=adapter)
    _require_mount(destination_root, read_only=False, adapter=adapter)
    if any(destination_root.iterdir()):
        raise RuntimeError("NAS-RECOVERY-DEST-RW-001 recovery destination 必須是新建空目錄")
    _verify_destination_writable(destination_root)

    if deployment_contract != image_contract:
        raise RuntimeError("NAS-RECOVERY-CONTRACT-001 recovery image contract mismatch")

    database_path = source_root / "inktime.db"
    session_key = source_root / "session.key"
    _require_regular_file(database_path, "NAS-RECOVERY-DB-001", "/source/inktime.db")
    _require_regular_file(session_key, "NAS-RECOVERY-SESSION-001", "/source/session.key")

    snapshot_handle = tempfile.NamedTemporaryFile(
        dir=destination_root,
        prefix=".source-snapshot-",
        suffix=".sqlite3",
        delete=False,
    )
    staged_snapshot = Path(snapshot_handle.name)
    snapshot_handle.close()
    archive: Path
    manifest: dict[str, object]
    try:
        _snapshot_read_only_database(database_path, staged_snapshot)
        service = BackupService(Database(staged_snapshot), destination_root)
        archive = service.create(include_secrets=True)
        manifest = service.validate(archive)
    finally:
        staged_snapshot.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", ".writer.lock", ".runtime.lock"):
            Path(f"{staged_snapshot}{suffix}").unlink(missing_ok=True)

    session_copy = destination_root / "session.key"
    session_digest = _copy_session_key(session_key, session_copy)
    metadata = {
        "recovery_contract_version": 1,
        "nas_deployment_contract": int(image_contract),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "previous_image_ref": previous_image_ref,
        "previous_image_digest": previous_image_digest,
        "target_image_ref": target_image_ref,
        "database_schema_version": int(str(manifest["database_schema_version"])),
        "backup_archive": archive.name,
        "backup_archive_sha256": _digest(archive),
        "session_key": session_copy.name,
        "session_key_sha256": session_digest,
        "secrets_policy": manifest["secrets_policy"],
        "backup_scope": manifest["backup_scope"],
        "source_mount": "read-only",
        "destination_mount": "bounded-read-write",
    }
    metadata_path = destination_root / "recovery-metadata.json"
    handle = tempfile.NamedTemporaryFile(
        dir=destination_root,
        prefix=".recovery-metadata-",
        mode="w",
        encoding="utf-8",
        delete=False,
    )
    staged_metadata = Path(handle.name)
    try:
        with handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged_metadata, 0o600)
        os.replace(staged_metadata, metadata_path)
    finally:
        staged_metadata.unlink(missing_ok=True)
    _fsync_directory(destination_root)
    return destination_root


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--destination-root", type=Path, required=True)
    parser.add_argument("--previous-image-ref", required=True)
    parser.add_argument("--previous-image-digest", required=True)
    parser.add_argument("--target-image-ref", required=True)
    parser.add_argument("--deployment-contract", required=True)
    args = parser.parse_args()

    image_contract = CONTRACT_FILE.read_text(encoding="utf-8").strip()
    recovery_dir = create_recovery(
        source_root=args.source_root,
        destination_root=args.destination_root,
        previous_image_ref=args.previous_image_ref,
        previous_image_digest=args.previous_image_digest,
        target_image_ref=args.target_image_ref,
        deployment_contract=args.deployment_contract,
        image_contract=image_contract,
    )
    print(f"RECOVERY_POINT={recovery_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
