import sqlite3

import pytest

from inktime.app.db import Database
import inktime.app.db.migrations as migrations
from inktime.app.repositories.devices import DeviceRepository
from inktime.app.repositories.resilience import ResilienceRepository
from tests.unit.test_resilience_repository import _seed_old_queue_item, _seed_queue_event


def test_upgrade_preserves_device_identity_queue_history_and_children(tmp_path, monkeypatch):
    full = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", tuple(m for m in full if m.version <= 63))
    database = Database(tmp_path / "v63.db")
    migrations.migrate(database)
    devices = DeviceRepository(database, "dummy-pepper")
    device_id, _ = devices.create("existing-device", fit_mode="cover")
    repository = ResilienceRepository(database)
    item = _seed_old_queue_item(database, repository, device_id, "v63", "2020-01-01T00:00:00+00:00")
    _seed_queue_event(database, item, device_id, "2020-01-01T00:00:00+00:00", "v63")
    before = dict(devices.get(device_id))
    monkeypatch.setattr(migrations, "MIGRATIONS", full)
    assert migrations.migrate(database) == [64, 65, 66, 67]
    assert dict(devices.get(device_id)) == before
    with database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM device_content_queue_events WHERE queue_item_id=?", (item,)).fetchone()[0] == 1
        assert connection.execute("SELECT status FROM device_content_queue_items WHERE id=?", (item,)).fetchone()[0] == "DISPLAYED"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.execute("UPDATE devices SET fit_mode='stretch_fill' WHERE id=?", (device_id,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE devices SET fit_mode='invalid' WHERE id=?", (device_id,))
    assert migrations.migrate(database) == []


def test_v65_upgrade_adds_caption_layout_and_preserves_tokens_children_and_schema_objects(tmp_path, monkeypatch):
    full = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", tuple(m for m in full if m.version <= 65))
    database = Database(tmp_path / "v65.db")
    migrations.migrate(database)
    devices = DeviceRepository(database, "dummy-pepper")
    device_id, token = devices.create("existing-pair", layout_mode="photo_pair", fit_mode="stretch_fill")
    repository = ResilienceRepository(database)
    item = _seed_old_queue_item(database, repository, device_id, "v65", "2020-01-01T00:00:00+00:00")
    _seed_queue_event(database, item, device_id, "2020-01-01T00:00:00+00:00", "v65")
    before = dict(devices.get(device_id))
    with database.transaction() as connection:
        connection.execute("CREATE TABLE device_name_audit(device_id TEXT,name TEXT)")
        connection.execute("CREATE VIEW v65_device_names AS SELECT id,name FROM devices")
        connection.execute("CREATE INDEX idx_v65_device_name ON devices(name) WHERE enabled=1")
        connection.execute("CREATE TRIGGER v65_device_name_change AFTER UPDATE OF name ON devices "
                           "BEGIN INSERT INTO device_name_audit VALUES(NEW.id,NEW.name); END")
    monkeypatch.setattr(migrations, "MIGRATIONS", full)
    assert migrations.migrate(database) == [66, 67]
    assert dict(devices.get(device_id)) == before
    with database.session() as connection:
        assert connection.execute("SELECT name FROM v65_device_names WHERE id=?", (device_id,)).fetchone()[0] == before["name"]
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='idx_v65_device_name'").fetchone()
        assert connection.execute("SELECT COUNT(*) FROM device_content_queue_events WHERE queue_item_id=?", (item,)).fetchone()[0] == 1
        assert connection.execute("SELECT device_id FROM device_content_queue_items WHERE id=?", (item,)).fetchone()[0] == device_id
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.execute("UPDATE devices SET name='new-name',layout_mode='photo_pair_caption' WHERE id=?", (device_id,))
        assert tuple(connection.execute("SELECT device_id,name FROM device_name_audit").fetchone()) == (device_id, "new-name")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE devices SET layout_mode='invalid' WHERE id=?", (device_id,))
    assert devices.authenticate(token, "127.0.0.1")["id"] == device_id
    assert migrations.migrate(database) == []
