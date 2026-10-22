"""Selection and upgrade edge cases; all data is created in pytest temp directories."""
from datetime import date

import pytest
from PIL import Image

from tests.conftest import create_admin
from tests.unit.test_local_selection import _candidate
from inktime.app.db import Database
from inktime.app.db import migrations
from inktime.app.domain.rendering.adaptive_layout import InsufficientPhotoContentError, pair_score, select_auto_photo_frames


def _settings(app, updates):
    actor = create_admin(app)
    app.extensions["inktime_settings_repository"].update_many(
        {"analysis.execution_mode": "local_only", "render.dither": "none", "render.layout": "auto_photo",
         "render.frame_orientation": "landscape", **updates}, changed_by=actor, source_ip="127.0.0.1",
    )
    return actor


def test_automatic_secondary_respects_disabled_history_fallback(app, tmp_path, monkeypatch):
    actor = _settings(app, {"render.history_today_fallback": "none"})
    root = tmp_path / "photos"
    root.mkdir()
    _candidate(app, root, "allowed-today", 90, "10-17")
    _candidate(app, root, "outside-history-policy", 90, "01-01")
    service = app.extensions["inktime_render_service"]
    selected = service.select_candidates_details(2, target_date=date(2026, 10, 17))
    assert [r["id"] for r in selected] == ["allowed-today"]
    observed = []

    def capture(plan, **kwargs):
        observed.append(plan)
        return Image.new("RGB", (480, 800), "white")

    monkeypatch.setattr(service, "_render_plan_image", capture)
    with pytest.raises(InsufficientPhotoContentError, match="RENDER-005"):
        service.publish([r["id"] for r in selected], actor, quantity_override=1, profile_keys=["safe_4c"])
    assert observed == []
    allowed = {r["id"] for r in selected}
    used = {p[k] for p in observed for k in ("primary_photo_id", "secondary_photo_id") if p[k]}
    assert used <= allowed, {"permitted": sorted(allowed), "published": sorted(used)}


def test_offline_day_searches_past_unpairable_high_ranked_burst(app, tmp_path, monkeypatch):
    actor = _settings(app, {"render.history_today_fallback": "ranked"})
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(24):
        _candidate(app, root, f"burst-{i:02d}", 100, "10-17")
    for i in range(12):
        _candidate(app, root, f"wide-{i:02d}", 30, "10-17")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET duplicate_group_id='same-burst' WHERE id LIKE 'burst-%'")
        connection.execute("UPDATE photos SET width=1600,height=900 WHERE id LIKE 'wide-%'")
    times = [f"{hour:02d}:25" for hour in range(0, 24, 2)]
    did, _ = app.extensions["inktime_device_repository"].create(
        "Burst library", panel_profile="safe_4c", delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True, schedule_times=times, offline_schedule_max_slots=16,
        frame_orientation="landscape", layout_mode="auto_photo",
    )
    service = app.extensions["inktime_display_preparation_service"]
    # Prove that the real selection policy permits enough complete frames.
    all_permitted = service.render_service.select_candidates_details(36, target_date=date(2026, 10, 17))
    assert all(row["width"] > 0 and row["height"] > 0 for row in all_permitted)
    assert all(row["visual_orientation_rotation_cw"] == 0 for row in all_permitted)
    assert len(select_auto_photo_frames(all_permitted, frame_orientation="landscape", count=12)) == 12
    published = []

    def publish(ids, *args, **kwargs):
        published.append(ids)
        return {"release_id": f"20261017-000000-{len(published):06d}"}

    monkeypatch.setattr(service.render_service, "publish", publish)
    monkeypatch.setattr(service.offline_schedules, "prepare_day", lambda **kwargs: {
        "schedule": {"schedule_times": times}, "slots": [{"time": time} for time in times],
    })
    result = service.prepare_device_day(device_id=did, target_date="2026-10-17", created_by=actor)
    assert result.get("status") == "ready", {"outcome": result, "published": len(published)}


def test_schema_70_restores_views_before_their_instead_of_triggers(tmp_path, monkeypatch):
    full = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", tuple(m for m in full if m.version <= 69))
    database = Database(tmp_path / "schema69.db")
    migrations.migrate(database)
    with database.transaction() as connection:
        connection.execute("CREATE VIEW custom_device_names AS SELECT id,name FROM devices")
        connection.execute("CREATE TRIGGER custom_view_update INSTEAD OF UPDATE OF name ON custom_device_names "
                           "BEGIN UPDATE devices SET name=NEW.name WHERE id=OLD.id; END")
    monkeypatch.setattr(migrations, "MIGRATIONS", full)
    assert migrations.migrate(database) == [migration.version for migration in full if migration.version > 69]
    with database.transaction() as connection:
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('custom_device_names','custom_view_update')"
        )}
        assert names == {"custom_device_names", "custom_view_update"}
        connection.execute("UPDATE custom_device_names SET name='Restored after migration'")


def test_greedy_auto_pairing_keeps_a_complete_valid_pair_assignment():
    def row(photo_id, **extra):
        return {"id": photo_id, "width": 900, "height": 1600, "captured_at": "2024-07-01T10:00:00+00:00", **extra}
    first = row("a", duplicate_group_id="unique-a")
    second = row("b", duplicate_group_id="duplicate-b-d", captured_at="2024-07-06T10:00:00+00:00")
    third = row("c", duplicate_group_id="unique-c")
    fourth = row("d", duplicate_group_id="duplicate-b-d", captured_at="2024-07-06T10:00:00+00:00")
    assert pair_score(first, second, desired_orientation="portrait") is not None
    assert pair_score(third, fourth, desired_orientation="portrait") is not None
    groups = select_auto_photo_frames([first, second, third, fourth], frame_orientation="landscape", count=2)
    assert len(groups) == 2, {"selected": [[r["id"] for r in group] for group in groups], "valid_complete_assignment": [["a", "b"], ["c", "d"]]}
