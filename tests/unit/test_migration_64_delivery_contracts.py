import sqlite3

import pytest

from inktime.app.db import Database
import inktime.app.db.migrations as migrations
from inktime.app.repositories.devices import DeviceRepository
from inktime.app.repositories.resilience import ResilienceRepository
from tests.unit.test_resilience_repository import _seed_old_queue_item, _seed_queue_event


def test_upgrade_preserves_device_identity_queue_history_and_children(tmp_path, monkeypatch):
    full = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", full[:-1])
    database = Database(tmp_path / "v63.db")
    migrations.migrate(database)
    devices = DeviceRepository(database, "dummy-pepper")
    device_id, _ = devices.create("existing-device", fit_mode="cover")
    repository = ResilienceRepository(database)
    item = _seed_old_queue_item(database, repository, device_id, "v63", "2020-01-01T00:00:00+00:00")
    _seed_queue_event(database, item, device_id, "2020-01-01T00:00:00+00:00", "v63")
    before = dict(devices.get(device_id))
    monkeypatch.setattr(migrations, "MIGRATIONS", full)
    assert migrations.migrate(database) == [64]
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
