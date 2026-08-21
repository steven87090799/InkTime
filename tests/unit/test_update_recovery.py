from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import zipfile

import pytest

from scripts.create_update_recovery import create_recovery


class RecoveryMounts:
    def __init__(self, source: Path, destination: Path, *, source_read_only: bool) -> None:
        self.source = source
        self.destination = destination
        self.source_read_only = source_read_only

    def mountinfo(self) -> str:
        source_mode = "ro" if self.source_read_only else "rw"
        return (
            "1 0 0:1 / / ro - ext4 /dev/root ro\n"
            f"2 1 0:2 / {self.source} {source_mode} - ext4 /dev/source {source_mode}\n"
            f"3 1 0:3 / {self.destination} rw - ext4 /dev/recovery rw"
        )


def _source_data(root: Path) -> tuple[Path, Path]:
    source = root / "source"
    destination = root / "recovery"
    source.mkdir()
    destination.mkdir()
    database = source / "inktime.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);
            INSERT INTO schema_migrations(version) VALUES (51);
            CREATE TABLE settings(
                key TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                value_json TEXT NOT NULL,
                value_type TEXT NOT NULL,
                requires_restart INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO settings VALUES (
                'analysis.execution_mode','analysis','"local_only"','string',0,
                '2026-08-21T00:00:00+00:00'
            );
            CREATE TABLE secrets(key TEXT PRIMARY KEY, value_encrypted TEXT NOT NULL);
            INSERT INTO secrets VALUES ('ci-secret','encrypted-ci-value');
            """
        )
        connection.commit()
    finally:
        connection.close()
    session_key = source / "session.key"
    session_key.write_text("unit-test-session-secret\n", encoding="utf-8")
    os.chmod(session_key, 0o600)
    return source, destination


def _create(source: Path, destination: Path, *, source_read_only: bool) -> Path:
    staged_snapshot = destination / ".source-snapshot.sqlite3"
    source_connection = sqlite3.connect(source / "inktime.db")
    target_connection = sqlite3.connect(staged_snapshot)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    return create_recovery(
        source_root=source,
        destination_root=destination,
        staged_snapshot=staged_snapshot,
        previous_image_ref="registry.example/inktime:v1.0.0",
        previous_image_digest="sha256:previous",
        target_image_ref="registry.example/inktime:v1.1.0",
        deployment_contract="3",
        image_contract="3",
        adapter=RecoveryMounts(
            source,
            destination,
            source_read_only=source_read_only,
        ),
    )


def test_recovery_refuses_writable_source_mount(tmp_path):
    source, destination = _source_data(tmp_path)

    with pytest.raises(RuntimeError, match="NAS-RECOVERY-SOURCE-RO-001"):
        _create(source, destination, source_read_only=False)

    assert [path.name for path in destination.iterdir()] == [".source-snapshot.sqlite3"]


def test_recovery_uses_readonly_source_and_bounded_writable_destination(tmp_path):
    source, destination = _source_data(tmp_path)
    database_digest = sha256((source / "inktime.db").read_bytes()).hexdigest()
    session_digest = sha256((source / "session.key").read_bytes()).hexdigest()
    source_entries = sorted(path.name for path in source.iterdir())

    assert _create(source, destination, source_read_only=True) == destination

    assert sha256((source / "inktime.db").read_bytes()).hexdigest() == database_digest
    assert sha256((source / "session.key").read_bytes()).hexdigest() == session_digest
    assert sorted(path.name for path in source.iterdir()) == source_entries
    assert not list(source.glob("inktime.db-*"))
    metadata = json.loads(
        (destination / "recovery-metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["nas_deployment_contract"] == 3
    assert metadata["source_mount"] == "read-only"
    assert metadata["destination_mount"] == "bounded-read-write"
    assert metadata["secrets_policy"] == "included"
    assert metadata["backup_scope"]["original_photos"] is False
    assert metadata["backup_scope"]["release_payloads"] is False
    assert stat_mode(destination / "session.key") == 0o600
    with zipfile.ZipFile(destination / metadata["backup_archive"]) as archive:
        assert set(archive.namelist()) == {
            "inktime.sqlite3",
            "settings.json",
            "manifest.json",
        }


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
