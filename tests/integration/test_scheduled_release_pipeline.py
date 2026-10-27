from __future__ import annotations

from PIL import Image
import pytest
import sqlite3

from inktime.app.domain.analysis.scoring import SEMANTIC_SCORE_KIND
from tests.unit.test_analysis_schema import valid_result


@pytest.mark.parametrize("wide,tall,expected", [(12, 0, 12), (0, 24, 12), (6, 12, 12), (0, 23, 0)])
def test_auto_photo_schedule_reserves_only_orientation_appropriate_sources(
    app, tmp_path, monkeypatch, custom_device_schedule, wide, tall, expected
):
    times = [f"{hour:02d}:25" for hour in range(0, 24, 2)]
    did, _ = app.extensions["inktime_device_repository"].create(
        "Automatic photo frame", panel_profile="gdep073e01_6c",
        delivery_mode="inktime_offline_schedule", offline_prefetch_allowed=True,
        schedule_times=times, offline_schedule_max_slots=16,
        frame_orientation="landscape", layout_mode="auto_photo", fit_mode="contain",
    )
    service = app.extensions["inktime_display_preparation_service"]
    rows = [
        {"id": f"wide-{i}", "width": 1600, "height": 900} for i in range(wide)
    ] + [{"id": f"tall-{i}", "width": 900, "height": 1600} for i in range(tall)]
    # Selection history has real photo foreign keys, even when publishing is stubbed.
    library = app.extensions["inktime_photo_repository"].ensure_library("候選照片", tmp_path)
    with app.extensions["inktime_database"].transaction() as connection:
        connection.executemany(
            "INSERT INTO photos(id,library_id,relative_path,width,height,created_at,updated_at) "
            "VALUES (?,?,?,?,?,datetime('now'),datetime('now'))",
            [(row["id"], library, f"{row['id']}.jpg", row["width"], row["height"]) for row in rows],
        )
    published = []

    def publish(ids, *args, **kwargs):
        config = kwargs["device_configs"][did]
        published.append((ids, config["layout_mode"]))
        assert config["_auto_photo"]
        return {"release_id": f"20261017-000000-{len(published):06d}"}

    monkeypatch.setattr(service.render_service, "select_candidates_details", lambda *args, **kwargs: rows)
    monkeypatch.setattr(service.render_service, "publish", publish)
    monkeypatch.setattr(service.offline_schedules, "prepare_day", lambda **kwargs: {
        "schedule": {"schedule_times": times}, "slots": [{"time": time} for time in times],
    })
    result = service.prepare_device_day(device_id=did, target_date="2026-10-17", created_by="test")
    assert len(published) == expected
    if expected:
        assert result["status"] == "ready"
        used = [photo for ids, _ in published for photo in ids]
        assert len(used) == len(set(used))
        for ids, layout in published:
            assert (layout == "full" and len(ids) == 1 and ids[0].startswith("wide-")) or (
                layout == "photo_pair" and len(ids) == 2 and all(photo.startswith("tall-") for photo in ids)
            )
    else:
        assert result["outcome"] == "no_content"


@pytest.mark.parametrize("available", [24, 23])
def test_two_hour_offline_pair_schedule_keeps_two_distinct_sources_per_frame(custom_device_schedule, app, monkeypatch, available):
    times = [f"{hour:02d}:00" for hour in range(0, 24, 2)]
    device_id, _ = app.extensions["inktime_device_repository"].create(
        "Two-hour paired frame", panel_profile="gdep073e01_6c",
        delivery_mode="inktime_offline_schedule", offline_prefetch_allowed=True,
        schedule_times=times, offline_schedule_max_slots=16,
        frame_orientation="landscape", layout_mode="photo_pair", fit_mode="contain",
    )
    service = app.extensions["inktime_display_preparation_service"]
    requested = []
    published = []

    def candidates(quantity, **kwargs):
        requested.append(quantity)
        return [{"id": f"source-{i}"} for i in range(available)]

    def publish(photo_ids, *args, **kwargs):
        published.append(photo_ids)
        return {"release_id": f"20261017-000000-{len(published):06d}"}

    monkeypatch.setattr(service.render_service, "select_candidates_details", candidates)
    monkeypatch.setattr(service.render_service, "publish", publish)
    monkeypatch.setattr(service.offline_schedules, "prepare_day", lambda **kwargs: {
        "schedule": {"schedule_times": times}, "slots": [{"time": time} for time in times],
    })
    result = service.prepare_device_day(device_id=device_id, target_date="2026-10-17", created_by="test")
    assert requested == [24]
    if available == 24:
        assert result["status"] == "ready"
        assert len(published) == 12 and all(len(ids) == 2 for ids in published)
        assert len({id for ids in published for id in ids}) == 24
    else:
        assert result["outcome"] == "no_content"
        assert published == []


def test_schedule_resolves_devices_limits_years_and_commits_history_after_publish(app, tmp_path):
    app.extensions["inktime_settings_repository"].update(
        "render.layout", "full", changed_by="test", source_ip="127.0.0.1"
    )
    app.extensions["inktime_settings_repository"].update(
        "analysis.execution_mode", "automatic_ai", changed_by="test", source_ip="127.0.0.1"
    )
    root = tmp_path / "scheduled"
    root.mkdir()
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("排程照片", root)
    now = "2026-07-22T00:00:00+00:00"
    for index, year in enumerate((2020, 2020, 2010), start=1):
        photo_id = f"scheduled-{index}"
        filename = f"{photo_id}.jpg"
        Image.new("RGB", (480, 800), (index * 40, 80, 120)).save(root / filename)
        with app.extensions["inktime_database"].session() as connection:
            connection.execute(
                """
                INSERT INTO photos(
                    id,library_id,relative_path,status,captured_at,captured_date,
                    captured_month_day,capture_date_status,eligible,lifecycle_status,
                    local_features_status,local_candidate_score,created_at,updated_at
                ) VALUES (?,?,?,'analyzed',?,?,?,'valid',1,'active','complete',?,?,?)
                """,
                (
                    photo_id,
                    library_id,
                    filename,
                    f"{year}-07-22T10:00:00",
                    f"{year}-07-22",
                    "07-22",
                    90 - index,
                    now,
                    now,
                ),
            )
        photos.save_analysis(
            photo_id,
            None,
            "single",
            "test-ai",
            "scheduled-test",
            valid_result(
                caption="這是一段排程測試說明文字。",
                types=["日常"],
                memory_score=90 - index,
                visual_score=80,
                side_caption="這是一段測試回憶短句。",
            ),
            "{}",
            score_kind=SEMANTIC_SCORE_KIND,
            ranking_score=90 - index,
            final_ranking_score=90 - index,
        )

    devices = app.extensions["inktime_device_repository"]
    safe_device, _ = devices.create("四色", panel_profile="safe_4c")
    six_device, _ = devices.create("六色", panel_profile="gdep073e01_6c")
    result = app.extensions["inktime_display_preparation_service"].prepare(
        {
            "display_times": ["08:00"],
            "lead_minutes": 30,
            "daily_count": 1,
            "device_ids": [safe_device, six_device],
            "candidate_years": [2020],
            "prefetch_count": 2,
            "ai_fallback": "use_existing",
            "render_fallback": "keep_current",
        },
        created_by="scheduled-test",
    )

    assert result["output_count"] == 2
    assert set(result["photo_ids"]) == {"scheduled-1", "scheduled-2"}
    with app.extensions["inktime_database"].session() as connection:
        releases = connection.execute(
            "SELECT id,render_profile,status FROM releases ORDER BY render_profile"
        ).fetchall()
        history = connection.execute("SELECT photo_id,release_id FROM display_history").fetchall()
    assert {row["render_profile"] for row in releases} == {"safe_4c", "gdep073e01_6c"}
    assert {row["status"] for row in releases} == {"published"}
    assert {row["release_id"] for row in history} == {row["id"] for row in releases}
    assert len(history) == 4


def test_enhanced_device_preparation_publishes_one_release_per_slot_and_is_idempotent(custom_device_schedule, app, tmp_path):
    app.extensions["inktime_settings_repository"].update(
        "analysis.execution_mode", "automatic_ai", changed_by="test", source_ip="127.0.0.1"
    )
    root = tmp_path / "offline-scheduled"
    root.mkdir()
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("離線排程照片", root)
    now = "2026-07-22T00:00:00+00:00"
    for index in range(1, 25):
        photo_id = f"offline-scheduled-{index}"
        filename = f"{photo_id}.jpg"
        Image.new("RGB", (800, 480), ((index * 10) % 256, 80, 120)).save(root / filename)
        with app.extensions["inktime_database"].session() as connection:
            connection.execute(
                """
                INSERT INTO photos(
                    id,library_id,relative_path,status,captured_at,captured_date,
                    captured_month_day,capture_date_status,eligible,lifecycle_status,
                    local_features_status,local_candidate_score,created_at,updated_at
                ) VALUES (?,?,?,'analyzed',?,?,?,'valid',1,'active','complete',?,?,?)
                """,
                (
                    photo_id,
                    library_id,
                    filename,
                    "2020-07-22T10:00:00",
                    "2020-07-22",
                    "07-22",
                    100 - index,
                    now,
                    now,
                ),
            )
        with app.extensions["inktime_database"].session() as connection:
            connection.execute("UPDATE photos SET width=800,height=480 WHERE id=?", (photo_id,))
        photos.save_analysis(
            photo_id,
            None,
            "single",
            "test-ai",
            "offline-scheduled-test",
            valid_result(
                caption="這是一段離線排程測試說明。",
                types=["日常"],
                memory_score=100 - index,
                visual_score=80,
                side_caption="這是一段測試回憶短句。",
            ),
            "{}",
            score_kind=SEMANTIC_SCORE_KIND,
            ranking_score=100 - index,
            final_ranking_score=100 - index,
        )

    device_id, _token = app.extensions["inktime_device_repository"].create(
        "Enhanced 離線相框",
        panel_profile="safe_4c",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=[f"{hour:02d}:00" for hour in range(24)],
        offline_schedule_max_slots=24,
        prefetch_lead_minutes=5,
    )
    service = app.extensions["inktime_display_preparation_service"]
    with app.extensions["inktime_database"].session() as connection:
        history_before = connection.execute(
            "SELECT COUNT(*) FROM display_history WHERE selection_method='offline_schedule_prepare'"
        ).fetchone()[0]
    prepared = service.prepare_device_day(
        device_id=device_id,
        target_date="2026-08-03",
        created_by="offline-scheduled-test",
    )
    repeated = service.prepare_device_day(
        device_id=device_id,
        target_date="2026-08-03",
        created_by="offline-scheduled-test",
    )

    assert prepared["status"] == "ready"
    assert prepared["idempotent"] is False
    assert len(prepared["slots"]) == 24
    assert repeated["idempotent"] is True
    with app.extensions["inktime_database"].session() as connection:
        release_count = connection.execute(
            "SELECT COUNT(*) FROM releases WHERE created_by='offline-scheduled-test'"
        ).fetchone()[0]
        queue_count = connection.execute(
            "SELECT COUNT(*) FROM device_content_queue_items WHERE device_id=? AND delivery_mode='offline_schedule'",
            (device_id,),
        ).fetchone()[0]
        history_after = connection.execute(
            "SELECT COUNT(*) FROM display_history WHERE selection_method='offline_schedule_prepare'"
        ).fetchone()[0]
    assert release_count == 24
    assert queue_count == 24
    assert history_before == 0
    assert history_after == 0


def test_enhanced_prepare_shortage_returns_no_content_without_partial_activation(app):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "沒有候選照片的離線相框",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00", "20:00"],
    )
    with app.extensions["inktime_database"].session() as connection:
        config_version = int(
            connection.execute(
                "SELECT config_version FROM devices WHERE id=?", (device_id,)
            ).fetchone()[0]
        )

    result = app.extensions["inktime_display_preparation_service"].prepare_device_day(
        device_id=device_id,
        target_date="2026-08-03",
        created_by="offline-shortage-test",
    )

    assert result["status"] == "completed"
    assert result["outcome_code"] == "NO_ELIGIBLE_CANDIDATES"
    assert result["output_count"] == 0
    with app.extensions["inktime_database"].session() as connection:
        schedules = connection.execute(
            """
            SELECT id,status,terminal_outcome_code,target_date,config_version
            FROM device_offline_schedules
            WHERE device_id=? AND target_date=? AND config_version=?
            """,
            (device_id, "2026-08-03", config_version),
        ).fetchall()
        assert len(schedules) == 1
        schedule = schedules[0]
        assert schedule["status"] == "failed"
        assert schedule["terminal_outcome_code"] == "NO_ELIGIBLE_CANDIDATES"
        assert schedule["target_date"] == "2026-08-03"
        assert schedule["config_version"] == config_version
        assert connection.execute(
            "SELECT COUNT(*) FROM device_offline_schedule_slots WHERE schedule_id=?",
            (schedule["id"],),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM device_content_queue_items WHERE device_id=?",
            (device_id,),
        ).fetchone()[0] == 0


def test_enhanced_prepare_fails_closed_when_device_config_changes_before_commit(custom_device_schedule, app, tmp_path, monkeypatch):
    app.extensions["inktime_settings_repository"].update(
        "analysis.execution_mode", "automatic_ai", changed_by="test", source_ip="127.0.0.1"
    )
    root = tmp_path / "offline-race"
    root.mkdir()
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("離線競態照片", root)
    photo_id = "offline-race-photo"
    filename = f"{photo_id}.jpg"
    Image.new("RGB", (480, 800), "white").save(root / filename)
    now = "2026-07-22T00:00:00+00:00"
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            INSERT INTO photos(
                id,library_id,relative_path,status,captured_at,captured_date,captured_month_day,
                capture_date_status,eligible,lifecycle_status,local_features_status,
                local_candidate_score,created_at,updated_at
            ) VALUES (?,?,?,'analyzed',?,?,?,'valid',1,'active','complete',?,?,?)
            """,
            (photo_id, library_id, filename, "2020-07-22", "2020-07-22", "07-22", 90, now, now),
        )
    photos.save_analysis(
        photo_id,
        None,
        "single",
        "test-ai",
        "offline-race",
        valid_result(
            caption="這是一段競態照片測試說明。",
            types=["日常"],
            memory_score=90,
            visual_score=90,
            side_caption="這是一段測試回憶短句。",
        ),
        "{}",
        score_kind=SEMANTIC_SCORE_KIND,
        ranking_score=90,
        final_ranking_score=90,
    )
    devices = app.extensions["inktime_device_repository"]
    device_id, _token = devices.create(
        "離線競態裝置",
        layout_mode="full", frame_orientation="portrait",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )
    with app.extensions["inktime_database"].session() as connection:
        start_version = int(
            connection.execute("SELECT config_version FROM devices WHERE id=?", (device_id,)).fetchone()[0]
        )
    repository = app.extensions["inktime_offline_schedule_repository"]
    original_prepare = repository.prepare_day

    def race_prepare(**kwargs):
        devices.update(
            device_id,
            name="離線競態裝置已變更",
            enabled=True,
            timezone_name="Asia/Taipei",
            schedule="08:00",
            schedule_times=["08:00"],
            delivery_mode="inktime_offline_schedule",
            offline_prefetch_allowed=True,
            rotation=180,
            panel_profile="safe_4c",
            prefetch_lead_minutes=5,
            button_wake_action="check_new",
        )
        return original_prepare(**kwargs)

    monkeypatch.setattr(repository, "prepare_day", race_prepare)
    with pytest.raises(ValueError, match="DISPLAY-CONFIG-RACE"):
        app.extensions["inktime_display_preparation_service"].prepare_device_day(
            device_id=device_id,
            target_date="2026-08-03",
            created_by="offline-race",
            expected_config_version=start_version,
        )
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM device_offline_schedules WHERE device_id=?", (device_id,)
        ).fetchone()[0] == 0
        release_state = connection.execute(
            "SELECT status,reconciliation_status FROM releases WHERE created_by='offline-race'"
        ).fetchone()
        failed_history = connection.execute(
            "SELECT COUNT(*) FROM display_history WHERE selection_method='offline_schedule_prepare'"
        ).fetchone()[0]
    assert release_state is not None
    assert tuple(release_state) == ("staged_failed", "aborted")
    assert release_state["status"] != "published"
    assert failed_history == 0


def test_release_coordinator_abort_staged_retains_payload_for_reconciliation(app):
    coordinator = app.extensions["inktime_release_coordinator"]
    publisher = app.extensions["inktime_release_publisher"]
    with pytest.raises(ValueError, match="沒有可發布"):
        coordinator.publish([], created_by="abort-test", photo_ids=[])
    staged = publisher.publish(
        [("abort-release", Image.new("RGB", (480, 800), "white"))],
        profile_key="safe_4c",
        activate=False,
    )
    coordinator.publish([staged], created_by="abort-test", photo_ids=[])

    coordinator.abort_staged([], "no-op")
    coordinator.abort_staged(
        [staged["release_id"], staged["release_id"], "missing-release"],
        "later offline slot failed",
    )

    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT status,reconciliation_status,failure_reason FROM releases WHERE id=?",
            (staged["release_id"],),
        ).fetchone()
    assert row is not None
    assert tuple(row) == ("staged_failed", "aborted", "later offline slot failed")
    assert publisher.validate(staged["release_id"])["release_id"] == staged["release_id"]


def test_release_coordinator_marks_payload_orphan_when_database_stage_fails(app):
    coordinator = app.extensions["inktime_release_coordinator"]
    publisher = app.extensions["inktime_release_publisher"]
    staged = publisher.publish(
        [("duplicate-stage", Image.new("RGB", (480, 800), "white"))],
        profile_key="safe_4c",
        activate=False,
    )
    coordinator.publish([staged], created_by="duplicate-stage", photo_ids=[])

    with pytest.raises(sqlite3.IntegrityError):
        coordinator.publish([staged], created_by="duplicate-stage", photo_ids=[])

    state_path = publisher.root / staged["release_id"] / ".inktime-state.json"
    assert state_path.is_file()
    assert '"status": "orphan"' in state_path.read_text(encoding="utf-8")
