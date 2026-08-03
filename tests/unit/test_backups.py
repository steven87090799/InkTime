from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from inktime.app.db import Database, RuntimeLockError, migrate
from inktime.app.bootstrap import bootstrap_services
from inktime.app.core.runtime_config import RuntimeConfig
from inktime.app.factory import create_app
from inktime.app.repositories.auth import AuthRepository
from inktime.app.repositories.devices import DeviceRepository
from inktime.app.repositories.resilience import ResilienceRepository
from inktime.app.services.backups import BackupService


def seed(database: Database, *, extra_photo: bool = False) -> None:
    with database.session() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO libraries(id,name,root_path,created_at,updated_at) VALUES ('lib','相簿','/photos',datetime('now'),datetime('now'))"
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO photos(
                id,library_id,relative_path,sha256,status,favorite,captured_at,captured_date,
                captured_month_day,capture_date_status,created_at,updated_at
            ) VALUES (
                'photo','lib','memory.jpg',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'analyzed',1,'2024-02-29T12:30:00','2024-02-29','02-29','valid',
                datetime('now'),datetime('now')
            )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO photo_analysis(
                id,photo_id,schema_version,stage,caption,types_json,raw_json,created_at
            ) VALUES (1,'photo',1,'high','重要分析','[]','{}',datetime('now'))
            """
        )
        connection.execute(
            "INSERT OR REPLACE INTO secrets(key,encrypted_value,updated_at) VALUES ('provider.test.api_key',?,datetime('now'))",
            (b"full-api-key-must-not-be-backed-up",),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO releases(
                id,display_type,width,height,pixel_format,manifest_json,status,created_at,render_profile
            ) VALUES ('release','epaper',480,800,'2bpp','{}','published',datetime('now'),'safe_4c')
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO display_history(photo_id,history_date,selection_method,release_id,displayed_at)
            VALUES ('photo','2020-07-22','random_history_day','release',datetime('now'))
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO jobs(
                id,kind,name,status,strategy,settings_json,created_at
            ) VALUES ('scheduled-backup','backup','排程備份','pending','local','{}',datetime('now'))
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO settings(
                key,category,value_json,value_type,requires_restart,updated_at
            ) VALUES ('backup.retention','備份','14','integer',0,datetime('now'))
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO settings_snapshots(
                id,created_at,actor_id,source_ip,reason,before_json,after_json,
                changed_keys_json,schema_version,application_version
            ) VALUES (
                'settings-snapshot',datetime('now'),'admin','127.0.0.1','備份測試',
                '{"backup.retention":14}','{"backup.retention":21}',
                '["backup.retention"]',1,'test'
            )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO settings_snapshot_items(
                snapshot_id,key,old_value_json,new_value_json,restored_default
            ) VALUES ('settings-snapshot','backup.retention','14','21',0)
            """
        )
        if extra_photo:
            connection.execute(
                """
                INSERT OR IGNORE INTO photos(id,library_id,relative_path,status,created_at,updated_at)
                VALUES ('extra','lib','extra.jpg','preprocessed',datetime('now'),datetime('now'))
                """
            )


def make_service(tmp_path: Path) -> tuple[Database, BackupService]:
    database = Database(tmp_path / "inktime.db")
    migrate(database)
    return database, BackupService(database, tmp_path / "backups")


def test_backup_excludes_secrets_and_restores_analysis_and_photo_state(tmp_path):
    database, service = make_service(tmp_path)
    seed(database)

    archive = service.create()
    manifest = service.validate(archive)

    assert manifest["backup_format_version"] == 2
    assert manifest["database_schema_version"] == 30
    assert manifest["secrets_policy"] == "excluded"
    assert manifest["important_table_counts"]["photos"] == 1
    assert manifest["important_table_counts"]["releases"] == 1
    assert manifest["important_table_counts"]["jobs"] == 1
    assert manifest["important_table_counts"]["display_history"] == 1
    assert manifest["important_table_counts"]["settings_snapshots"] == 1
    assert manifest["important_table_counts"]["settings_snapshot_items"] == 1
    with zipfile.ZipFile(archive) as bundle:
        backed_up_database = tmp_path / "backed-up.sqlite3"
        backed_up_database.write_bytes(bundle.read("inktime.sqlite3"))
        settings = json.loads(bundle.read("settings.json"))
    with Database(backed_up_database).session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM settings_snapshots").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM settings_snapshot_items").fetchone()[0] == 1
    assert settings["secrets_included"] is False
    assert settings["settings"][0]["key"] == "backup.retention"
    assert b"full-api-key-must-not-be-backed-up" not in backed_up_database.read_bytes()

    with database.session() as connection:
        connection.execute(
            "UPDATE photos SET captured_at=NULL,captured_date=NULL,captured_month_day=NULL,"
            "capture_date_status='missing' WHERE id='photo'"
        )
    seed(database, extra_photo=True)
    restored = service.restore(archive)

    assert restored["schema_version"] == 30
    assert Path(restored["safety_copy"]).is_file()
    with database.session() as connection:
        photo = connection.execute("SELECT favorite,status FROM photos WHERE id='photo'").fetchone()
        assert tuple(photo) == (1, "analyzed")
        assert connection.execute("SELECT caption FROM photo_analysis").fetchone()[0] == "重要分析"
        assert connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM display_history").fetchone()[0] == 1
        assert connection.execute("SELECT status FROM jobs").fetchone()[0] == "pending"
        assert connection.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0
        captured = connection.execute(
            "SELECT captured_at,captured_date,captured_month_day,capture_date_status "
            "FROM photos WHERE id='photo'"
        ).fetchone()
        assert tuple(captured) == (
            "2024-02-29T12:30:00",
            "2024-02-29",
            "02-29",
            "valid",
        )
        assert (
            connection.execute(
                "SELECT id FROM photos INDEXED BY idx_photos_captured_month_day "
                "WHERE captured_month_day='02-29'"
            ).fetchone()[0]
            == "photo"
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not list(tmp_path.glob(".inktime-restore-*"))


def test_corrupt_backup_never_overwrites_current_database(tmp_path):
    database, service = make_service(tmp_path)
    seed(database)
    archive = service.create()
    corrupt = tmp_path / "backups" / "inktime-backup-corrupt.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(corrupt, "w") as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "inktime.sqlite3":
                payload = payload[: max(1, len(payload) // 2)]
            target.writestr(name, payload)

    with pytest.raises(ValueError, match="BACKUP-003"):
        service.restore(corrupt)

    with database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1
        assert connection.execute("SELECT caption FROM photo_analysis").fetchone()[0] == "重要分析"


def test_restore_requires_all_runtime_processes_to_be_stopped(tmp_path):
    database, service = make_service(tmp_path)
    seed(database)
    archive = service.create()
    runtime_lock = database.acquire_runtime_lock(exclusive=False)
    try:
        with pytest.raises(RuntimeLockError, match="RESTORE-001"):
            service.restore(archive)
    finally:
        runtime_lock.close()
    with database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1


def test_post_replace_validation_failure_automatically_recovers_current_database(monkeypatch, tmp_path):
    database, service = make_service(tmp_path)
    seed(database)
    archive = service.create()
    seed(database, extra_photo=True)
    original_validate = service._validate_restore_database
    calls = 0

    def fail_after_replace(path, manifest=None, *, require_platform_tables=True):
        nonlocal calls
        calls += 1
        if path == database.path and calls >= 2:
            raise ValueError("forced post-restore failure")
        return original_validate(path, manifest, require_platform_tables=require_platform_tables)

    monkeypatch.setattr(service, "_validate_restore_database", fail_after_replace)
    with pytest.raises(ValueError, match="forced post-restore failure"):
        service.restore(archive)

    with database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 2
        assert connection.execute("SELECT caption FROM photo_analysis").fetchone()[0] == "重要分析"


def test_empty_sqlite_snapshot_is_rejected_before_current_database_is_touched(tmp_path):
    database, service = make_service(tmp_path)
    seed(database)
    invalid = tmp_path / "empty.sqlite3"
    sqlite3.connect(invalid).close()

    with pytest.raises(ValueError, match="RESTORE-002"):
        service.restore_sqlite_snapshot(invalid)

    with database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1
        assert connection.execute("SELECT caption FROM photo_analysis").fetchone()[0] == "重要分析"


def test_fresh_volume_restore_preserves_platform_identity_and_roles(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source_database = Database(source / "inktime.db")
    migrate(source_database)
    seed(source_database)
    AuthRepository(source_database).create_initial_administrator("restore-admin", "restore-test-passphrase")
    source_devices = DeviceRepository(source_database, "restore-pepper")
    device_id, device_token = source_devices.create("restore-device")
    source_queue = ResilienceRepository(source_database)
    source_queue.ensure_queue(device_id)
    queue_item = source_queue.enqueue_release(device_id=device_id, release_id="release")
    with source_database.session() as connection:
        connection.execute("UPDATE settings SET value_json='21' WHERE key='backup.retention'")

    archive = BackupService(source_database, source / "backups").create()
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    restored_database = Database(fresh / "inktime.db")
    migrate(restored_database)
    restored = BackupService(restored_database, fresh / "backups").restore(archive)

    assert restored["schema_version"] == 30
    assert AuthRepository(restored_database).authenticate("restore-admin", "restore-test-passphrase")
    assert DeviceRepository(restored_database, "restore-pepper").authenticate(device_token, "203.0.113.20")
    with restored_database.session() as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 30
        assert connection.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT release_id FROM device_content_queue_items WHERE id=?",
                (queue_item["id"],),
            ).fetchone()[0]
            == "release"
        )
        assert (
            connection.execute("SELECT value_json FROM settings WHERE key='backup.retention'").fetchone()[0]
            == "21"
        )

    runtime = RuntimeConfig.from_sources(
        environ={},
        base_dir=fresh,
        environment="test",
        database_path=fresh / "inktime.db",
        data_dir=fresh,
        release_dir=fresh / "releases",
        backup_dir=fresh / "backups",
        cache_dir=fresh / "cache",
        photo_dir=fresh / "photos",
        testing=True,
        development=False,
        legacy_enabled=False,
        cookie_secure=False,
    )
    app = create_app(runtime)
    client = app.test_client()
    client.get("/login")
    with client.session_transaction() as session:
        csrf_token = str(session["csrf_token"])
    response = client.post(
        "/login",
        data={
            "username": "restore-admin",
            "password": "restore-test-passphrase",
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session.get("user_id")
    app.extensions["inktime_service_container"].close()

    worker = bootstrap_services(runtime, role="worker")
    scheduler = bootstrap_services(runtime, role="scheduler")
    try:
        assert worker.role == "worker"
        assert scheduler.role == "scheduler"
    finally:
        scheduler.close()
        worker.close()
