from __future__ import annotations

from datetime import date
import json

from PIL import Image
import pytest

from inktime.app.repositories.display_history import DisplayHistoryRepository
from tests.conftest import create_admin, login
from tests.unit.test_analysis_schema import valid_result


def _setting(app, key, value):
    app.extensions["inktime_settings_repository"].update(key, value, changed_by="test", source_ip="test")


def _photo(app, root, identifier, score=90, month_day="10-22"):
    root.mkdir(exist_ok=True)
    Image.new("RGB", (800, 600), "white").save(root / f"{identifier}.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library = photos.ensure_library("輪替照片", root)
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            """INSERT INTO photos(id,library_id,relative_path,width,height,status,eligible,lifecycle_status,
                exclusion_status,local_features_status,local_candidate_score,captured_at,captured_date,
                captured_month_day,crop_focus_x,crop_focus_y,e6_score,created_at,updated_at)
                VALUES (?,?,?,800,600,'analyzed',1,'active','eligible','complete',90,?,?,?,0.5,0.5,90,?,?)""",
            (identifier, library, f"{identifier}.jpg", f"2020-{month_day}T10:00:00", f"2020-{month_day}",
             month_day, "2026-01-01", "2026-01-01"),
        )
    photos.save_analysis(
        identifier, None, "single", "test-ai", "test", valid_result(types=["其他"], memory_score=score, visual_score=score), "{}",
        score_kind="semantic", ranking_score=score, final_ranking_score=score,
    )


def _seen(app, identifier, at="2025-10-22T00:00:00+00:00"):
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            """INSERT INTO display_history(photo_id,history_date,selection_method,displayed_at)
            VALUES (?,?,'device_queue_ack',?)""", (identifier, at[:10], at),
        )


def test_seasonal_window_rotates_unused_and_keeps_complete_history(app, tmp_path):
    root = tmp_path / "seasonal-rotation"
    _photo(app, root, "seen-in-window", 99, "12-31")
    _photo(app, root, "unused-in-window", 50, "01-02")
    _photo(app, root, "unused-outside", 100, "10-22")
    _seen(app, "seen-in-window")
    _setting(app, "analysis.execution_mode", "seasonal_ai")
    _setting(app, "analysis.seasonal_window_days", 2)
    _setting(app, "render.seasonal_outside_fallback", False)
    _setting(app, "render.selection_mode", "top_ranked")
    service = app.extensions["inktime_render_service"]
    rows = service.select_candidates_details(1, target_date=date(2027, 1, 1), record_history=True)
    assert [row["id"] for row in rows] == ["seen-in-window"]
    run = DisplayHistoryRepository(app.extensions["inktime_database"]).detail(rows[0]["selection_run_id"])
    assert run["target_date"] == "2027-01-01"
    assert {row["photo_id"] for row in run["candidates"]} == {"seen-in-window", "unused-in-window"}
    assert {row["snapshot"]["match_type"] for row in run["candidates"]} == {"nearby_day", "seasonal_low_score"}
    assert [row["id"] for row in service.select_auto_photo_candidates(
        1, frame_orientation="landscape", target_date=date(2027, 1, 1),
    )] == ["seen-in-window"]


def test_auto_pair_preview_does_not_create_production_history(app, tmp_path):
    _photo(app, tmp_path / "preview", "preview")
    service = app.extensions["inktime_render_service"]
    primary = dict(app.extensions["inktime_photo_repository"].get_with_path("preview"))
    service._select_auto_photo_pair_candidate(primary, frame_orientation="portrait")
    assert DisplayHistoryRepository(app.extensions["inktime_database"]).list()["total"] == 0


def test_large_auto_pool_returns_only_actual_frame_sources(app, tmp_path, monkeypatch):
    _photo(app, tmp_path / "pool", "pool")
    service = app.extensions["inktime_render_service"]
    rows = [{**dict(app.extensions["inktime_photo_repository"].get_with_path("pool")),
             "id": f"source-{index}", "match_type": "seasonal_window"} for index in range(300)]
    monkeypatch.setattr(service, "_auto_photo_candidate_pool", lambda **kwargs: rows)
    selected = service.select_auto_photo_candidates(2, frame_orientation="landscape")
    assert [row["id"] for row in selected] == ["source-0", "source-1"]


def test_seasonal_adaptive_pair_uses_future_target_window(app, tmp_path):
    root = tmp_path / "future-pair"
    _photo(app, root, "primary", 90, "01-01")
    _photo(app, root, "inside", 70, "01-02")
    _photo(app, root, "outside", 99, "10-22")
    _setting(app, "analysis.execution_mode", "seasonal_ai")
    _setting(app, "analysis.seasonal_window_days", 2)
    _setting(app, "render.seasonal_outside_fallback", False)
    service = app.extensions["inktime_render_service"]
    primary = dict(app.extensions["inktime_photo_repository"].get_with_path("primary"))
    secondary = service._select_adaptive_pair_candidate(
        primary, frame_orientation="portrait", target_date=date(2027, 1, 1),
    )
    assert secondary["id"] == "inside"
    assert DisplayHistoryRepository(app.extensions["inktime_database"]).list()["total"] == 0


def test_multiple_orientation_runs_and_late_pair_source_count_once(app, tmp_path):
    root = tmp_path / "orientations"
    for identifier in ("primary", "secondary"):
        _photo(app, root, identifier)
    database = app.extensions["inktime_database"]
    repository = DisplayHistoryRepository(database)
    primary = dict(app.extensions["inktime_photo_repository"].get_with_path("primary"))
    runs = [repository.record_selection(target=date(2026, 10, 22), candidates=[primary], selected=[primary],
                                         method="automatic") for _ in range(2)]
    staged = app.extensions["inktime_release_publisher"].publish(
        [("primary+secondary", Image.new("RGB", (480, 800), "white"))], profile_key="safe_4c", activate=False,
    )
    published = app.extensions["inktime_release_coordinator"].publish(
        [staged], created_by="test", photo_ids=["primary", "secondary"], selection_run_ids=runs,
    )
    repository.attach_releases(runs, published, ["primary", "secondary"])
    for run_id in runs:
        assert set(repository.detail(run_id)["published_photo_ids"]) == {"primary", "secondary"}
    with database.session() as connection:
        assert [row[0] for row in connection.execute("SELECT selection_count FROM photos ORDER BY id")] == [1, 1]


def test_next_year_prefers_unseen_lower_score_and_rotates_least_shown(app, tmp_path):
    _setting(app, "analysis.execution_mode", "local_with_manual_ai")
    root = tmp_path / "rotation"
    for identifier, score in (("A", 99), ("B", 50), ("C", 70)):
        _photo(app, root, identifier, score)
    _setting(app, "render.selection_mode", "history_today")
    _setting(app, "render.history_today_fallback", "none")
    _seen(app, "A")
    service = app.extensions["inktime_render_service"]
    assert service.select_candidates_details(1, target_date=date(2027, 10, 22))[0]["id"] == "C"
    _seen(app, "C", "2026-10-22T00:00:00+00:00")
    assert service.select_candidates_details(1, target_date=date(2027, 10, 22))[0]["id"] == "B"
    _seen(app, "B", "2024-10-22T00:00:00+00:00")
    _seen(app, "A", "2026-10-22T00:00:00+00:00")
    assert service.select_candidates_details(1, target_date=date(2027, 10, 22))[0]["id"] == "B"


def test_unseen_date_fallback_precedes_repeat_but_respects_disabled_fallback(app, tmp_path):
    _setting(app, "analysis.execution_mode", "local_with_manual_ai")
    root = tmp_path / "dates"
    _photo(app, root, "exact", 99)
    _photo(app, root, "nearby", 50, "10-23")
    _seen(app, "exact")
    _setting(app, "render.selection_mode", "history_today")
    _setting(app, "render.history_today_window_days", 7)
    _setting(app, "render.history_today_fallback", "nearby_only")
    service = app.extensions["inktime_render_service"]
    assert service.select_candidates_details(1, target_date=date(2026, 10, 22))[0]["id"] == "nearby"
    _setting(app, "render.history_today_fallback", "none")
    assert service.select_candidates_details(1, target_date=date(2026, 10, 22))[0]["id"] == "exact"


def test_bounded_sql_pool_cannot_hide_unseen_photo_behind_used_high_scores(app, tmp_path):
    _setting(app, "analysis.execution_mode", "local_with_manual_ai")
    root = tmp_path / "bounded"
    for index in range(105):
        identifier = f"used-{index}"
        _photo(app, root, identifier, 99)
        _seen(app, identifier)
    _photo(app, root, "unseen", 10)
    _setting(app, "render.selection_mode", "history_today")
    _setting(app, "render.history_today_fallback", "none")
    selected = app.extensions["inktime_render_service"].select_candidates_details(1, target_date=date(2026, 10, 22))
    assert selected[0]["id"] == "unseen"


def test_history_keeps_all_candidates_over_fifty_and_freezes_names(client, app, tmp_path):
    root = tmp_path / "snapshots"
    for index in range(75):
        _photo(app, root, f"snapshot-{index:03}")
    _setting(app, "render.selection_mode", "history_today")
    _setting(app, "render.history_today_fallback", "none")
    selected = app.extensions["inktime_render_service"].select_candidates_details(
        2, target_date=date(2026, 10, 22), record_history=True,
    )
    run_id = selected[0]["selection_run_id"]
    create_admin(app)
    assert client.get("/api/v1/display-history").status_code == 401
    login(client)
    first = client.get(f"/api/v1/display-history/{run_id}").get_json()
    second = client.get(f"/api/v1/display-history/{run_id}?page=2").get_json()
    assert first["candidate_count"] == 75
    assert len(first["candidates"]) == 60
    assert len(second["candidates"]) == 15
    assert sum(row["selected"] for row in first["candidates"]) == 2
    identifier = first["candidates"][0]["photo_id"]
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute("UPDATE photos SET relative_path='renamed.jpg' WHERE id=?", (identifier,))
    refreshed = client.get(f"/api/v1/display-history/{run_id}").get_json()
    assert refreshed["candidates"][0]["snapshot"]["relative_path"] == f"{identifier}.jpg"
    assert client.get(f"/display-history/{run_id}").status_code == 200
    assert client.get("/display-history?date=2026-10-22").status_code == 200
    assert client.get("/api/v1/display-history?date=2026-10-23").get_json()["total"] == 0
    assert client.get("/api/v1/display-history?date=invalid").status_code == 400


def _publish_pair(app, root):
    for identifier in ("pair-a", "pair-b"):
        _photo(app, root, identifier)
    repository = DisplayHistoryRepository(app.extensions["inktime_database"])
    candidates = [dict(app.extensions["inktime_photo_repository"].get_with_path(identifier))
                  for identifier in ("pair-a", "pair-b")]
    run_id = repository.record_selection(
        target=date(2026, 10, 22), candidates=candidates, selected=candidates, method="automatic",
    )
    staged = app.extensions["inktime_release_publisher"].publish(
        [("pair-a+pair-b", Image.new("RGB", (480, 800), "white"))],
        profile_key="safe_4c", activate=False,
    )
    release = app.extensions["inktime_release_coordinator"].publish(
        [staged], created_by="test", photo_ids=["pair-a", "pair-b"], selection_run_ids=[run_id],
    )[0]
    return repository, run_id, release


def test_publication_counts_selection_and_each_device_ack_counts_both_sources_once(app, tmp_path):
    repository, run_id, release = _publish_pair(app, tmp_path / "pair")
    photos = app.extensions["inktime_photo_repository"]
    assert photos.get_with_path("pair-a")["selection_count"] == 1
    assert photos.get_with_path("pair-a")["display_count"] == 0
    # Reconciliation/repeated attachment cannot count publication twice.
    repository.attach_releases([run_id], [release], ["pair-a", "pair-b"])
    queue = app.extensions["inktime_resilience_repository"]
    for index in range(2):
        device_id, _token = app.extensions["inktime_device_repository"].create(f"Frame {index}")
        queue.ensure_queue(device_id, depth=3)
        item = queue.enqueue_release(device_id=device_id, release_id=release["release_id"])
        version = queue.queue(device_id)["queue"]["queue_version"]
        for event in ("MANIFEST_RECEIVED", "DOWNLOAD_COMPLETED", "HASH_VERIFIED", "DISPLAY_COMPLETED", "DISPLAY_COMPLETED"):
            queue.queue_ack(device_id=device_id, payload={
                "queue_item_id": item["id"], "queue_version": version,
                "event": event, "idempotency_key": f"{index}-{event}",
            })
    for identifier in ("pair-a", "pair-b"):
        photo = photos.get_with_path(identifier)
        assert photo["display_count"] == 2
        assert photo["selection_count"] == 1
    assert repository.detail(run_id)["releases"][0]["confirmed_count"] == 4
    assert repository.list(tab="displays")["total"] == 4


def test_legacy_without_ack_uses_selected_count_for_rotation(app, tmp_path):
    _publish_pair(app, tmp_path / "legacy")
    _photo(app, tmp_path / "legacy", "fresh", 20)
    _setting(app, "render.selection_mode", "top_ranked")
    selected = app.extensions["inktime_render_service"].select_candidates_details(1)
    assert selected[0]["id"] == "fresh"


def test_failed_release_keeps_snapshot_and_does_not_increment_selection(app, tmp_path, monkeypatch):
    coordinator = app.extensions["inktime_release_coordinator"]
    def fail_activation(_manifests):
        raise OSError("activation")

    monkeypatch.setattr(coordinator.publisher, "activate_manifests", fail_activation)
    with pytest.raises(OSError, match="activation"):
        _publish_pair(app, tmp_path / "failed")
    repository = DisplayHistoryRepository(app.extensions["inktime_database"])
    run = repository.list()["items"][0]
    assert run["candidate_count"] == 2
    assert run["release_count"] == 0
    assert json.loads(run["published_photo_ids_json"]) == []
    assert app.extensions["inktime_photo_repository"].get_with_path("pair-a")["selection_count"] == 0


def test_seasonal_day_priority_quality_and_cross_year_rotation(app, tmp_path):
    root = tmp_path / "day-priority"
    for identifier, score, day in (("same-old-year", 75, "10-22"), ("same-poor", 40, "10-22"),
                                    ("previous-day", 65, "10-21"), ("following-day", 99, "10-23"),
                                    ("outside", 100, "10-16")):
        _photo(app, root, identifier, score, day)
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute("UPDATE photos SET captured_date='2012-10-22',captured_at='2012-10-22' WHERE id='same-old-year'")
    _setting(app, "analysis.execution_mode", "seasonal_ai")
    service = app.extensions["inktime_render_service"]
    def chosen(target):
        return [row["id"] for row in service.select_auto_photo_candidates(1, frame_orientation="landscape", target_date=target)]
    target = date(2026, 10, 22)
    assert set(chosen(target)) <= {"same-old-year", "previous-day", "following-day"}
    assert chosen(target) == chosen(target)
    _seen(app, "same-old-year", "2026-10-22T00:00:00+00:00")
    assert "same-poor" not in chosen(target)
    assert "outside" not in chosen(date(2027, 10, 22))
    _setting(app, "render.seasonal_priority_min_score", 80)
    assert chosen(target) == ["following-day"]


def test_copied_content_cannot_reset_the_rotation_counter(app, tmp_path):
    root = tmp_path / "copied-content"
    for identifier, score in (("shown", 95), ("copy", 95), ("fresh", 65)):
        _photo(app, root, identifier, score)
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute("UPDATE photos SET sha256=? WHERE id IN ('shown','copy')", ("a" * 64,))
        connection.execute("UPDATE photos SET sha256=? WHERE id='fresh'", ("b" * 64,))
    _seen(app, "shown")
    _setting(app, "analysis.execution_mode", "seasonal_ai")
    rows = app.extensions["inktime_render_service"].select_candidates_details(3, target_date=date(2027, 10, 22))
    assert len(rows) == 2
    assert next(row for row in rows if row["id"] != "fresh")["exposure_count"] == 1
    _seen(app, "copy")
    rows = app.extensions["inktime_render_service"].select_candidates_details(3, target_date=date(2027, 10, 22))
    assert next(row for row in rows if row["id"] != "fresh")["exposure_count"] == 2


@pytest.mark.parametrize("device_specific", [False, True])
def test_final_publication_keeps_cross_year_pair_priorities(app, tmp_path, monkeypatch, device_specific):
    actor = create_admin(app)
    root = tmp_path / "published-priorities"
    for identifier, captured in (("same-2012", "2012-10-22"), ("same-2019", "2019-10-22"),
                                ("near-2012", "2012-10-21"), ("other-2012", "2012-10-20")):
        _photo(app, root, identifier, 80, captured[5:])
        Image.new("RGB", (600, 900), "white").save(root / f"{identifier}.jpg")
        with app.extensions["inktime_database"].transaction() as connection:
            connection.execute("UPDATE photos SET width=600,height=900,captured_date=?,captured_at=? WHERE id=?",
                               (captured, captured, identifier))
    for key, value in {"analysis.execution_mode": "seasonal_ai", "render.layout": "auto_photo",
                       "render.frame_orientation": "landscape", "render.dither": "none"}.items():
        _setting(app, key, value)
    service = app.extensions["inktime_render_service"]
    observed = []
    def capture(plan, **kwargs):
        observed.append([plan["primary_photo_id"], plan["secondary_photo_id"]])
        return Image.new("RGB", (480, 800), "white")
    monkeypatch.setattr(service, "_render_plan_image", capture)
    devices = None
    if device_specific:
        identifier, _ = app.extensions["inktime_device_repository"].create(
            "Seasonal pairs", panel_profile="safe_4c", layout_mode="auto_photo", frame_orientation="landscape",
        )
        devices = [identifier]
    service.publish([], actor, profile_keys=["safe_4c"], device_ids=devices, quantity_override=2,
                    target_date=date(2026, 10, 22))
    assert observed == [["same-2012", "same-2019"], ["near-2012", "other-2012"]]


def test_empty_season_uses_labeled_high_score_fallback(app, tmp_path):
    root = tmp_path / "outside-fallback"
    _photo(app, root, "low-seasonal", 30, "10-22")
    _photo(app, root, "outside-high", 90, "04-01")
    _setting(app, "analysis.execution_mode", "seasonal_ai")
    _setting(app, "analysis.seasonal_window_days", 5)
    _setting(app, "render.seasonal_outside_fallback", True)
    rows = app.extensions["inktime_render_service"].select_candidates_details(
        1, target_date=date(2027, 10, 22), record_history=True)
    assert rows[0]["id"] == "outside-high"
    run = DisplayHistoryRepository(app.extensions["inktime_database"]).detail(rows[0]["selection_run_id"])
    chosen = next(row for row in run["candidates"] if row["selected"])
    assert chosen["snapshot"]["match_type"] == "ranked_fallback"
    assert chosen["snapshot"]["captured_month_day"] == "04-01"
