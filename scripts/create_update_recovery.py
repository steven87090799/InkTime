#!/usr/bin/env python3
"""Create a verified, online NAS recovery point before container replacement."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from inktime.app.db import Database
from inktime.app.services.backups import BackupService


DATA_ROOT = Path("/data")
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


def _copy_session_key(source: Path, destination: Path) -> str:
    details = os.lstat(source)
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise RuntimeError("NAS-RECOVERY-SESSION-001 /data/session.key 必須是一般檔案且不可為 symlink")
    if stat.S_IMODE(details.st_mode) != 0o600:
        raise RuntimeError("NAS-RECOVERY-SESSION-002 /data/session.key 權限必須為 0600")
    handle = tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".session-key-", delete=False)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous-image-ref", required=True)
    parser.add_argument("--previous-image-digest", required=True)
    parser.add_argument("--target-image-ref", required=True)
    parser.add_argument("--deployment-contract", required=True)
    args = parser.parse_args()

    image_contract = CONTRACT_FILE.read_text(encoding="utf-8").strip()
    if args.deployment_contract != image_contract:
        raise RuntimeError("NAS-RECOVERY-CONTRACT-001 recovery image contract mismatch")

    database_path = DATA_ROOT / "inktime.db"
    session_key = DATA_ROOT / "session.key"
    if not database_path.is_file() or database_path.is_symlink():
        raise RuntimeError("NAS-RECOVERY-DB-001 /data/inktime.db 必須是既有的一般檔案")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    recovery_dir = DATA_ROOT / "backups" / f"update-recovery-{stamp}"
    recovery_dir.mkdir(parents=True, mode=0o700)
    os.chmod(recovery_dir, 0o700)

    service = BackupService(Database(database_path), recovery_dir)
    archive = service.create(include_secrets=True)
    manifest = service.validate(archive)
    session_copy = recovery_dir / "session.key"
    session_digest = _copy_session_key(session_key, session_copy)
    metadata = {
        "recovery_contract_version": 1,
        "nas_deployment_contract": int(image_contract),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "previous_image_ref": args.previous_image_ref,
        "previous_image_digest": args.previous_image_digest,
        "target_image_ref": args.target_image_ref,
        "database_schema_version": int(manifest["database_schema_version"]),
        "backup_archive": archive.name,
        "backup_archive_sha256": _digest(archive),
        "session_key": session_copy.name,
        "session_key_sha256": session_digest,
        "secrets_policy": manifest["secrets_policy"],
        "backup_scope": manifest["backup_scope"],
    }
    metadata_path = recovery_dir / "recovery-metadata.json"
    handle = tempfile.NamedTemporaryFile(
        dir=recovery_dir, prefix=".recovery-metadata-", mode="w", encoding="utf-8", delete=False
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
    _fsync_directory(recovery_dir)
    _fsync_directory(recovery_dir.parent)
    print(f"RECOVERY_POINT={recovery_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
