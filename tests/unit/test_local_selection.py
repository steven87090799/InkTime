from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from PIL import Image

from inktime.app.services.local_selection import LocalSelectionPolicy


def _candidate(app, root: Path, photo_id: str, score: float, month_day: str):
    Image.new("RGB", (900, 1600), "#4477aa").save(root / f"{photo_id}.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("本機選片", root)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """INSERT INTO photos(id,library_id,relative_path,width,height,status,eligible,lifecycle_status,exclusion_status,
                local_features_status,local_candidate_score,captured_at,captured_date,captured_month_day,created_at,updated_at)
                VALUES (?,?,?,?,?,'analyzed',1,'active','eligible','complete',?,?,?,?,?,?)""",
            (photo_id, library_id, f"{photo_id}.jpg", 900, 1600, score, f"2020-{month_day}T10:00:00+00:00", f"2020-{month_day}", month_day, "2020-01-01", "2020-01-01"),
        )


def test_local_policy_is_bounded_deterministic_and_pairs_different_photos(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _candidate(app, root, "a", 90, "07-28")
    _candidate(app, root, "b", 80, "07-28")
    _candidate(app, root, "c", 99, "01-01")
    policy = LocalSelectionPolicy(app.extensions["inktime_database"], app.extensions["inktime_settings_repository"], app.extensions["inktime_resilience_repository"])
    result = policy.select(target=date(2026, 7, 28), orientation="portrait", quantity=2, layout="photo_pair")
    assert [row["id"] for row in result["selected"][:2]] == ["a", "b"]
    assert result["selected"][0]["id"] != result["selected"][1]["id"]
    assert "base_local_score" in result["selected"][0]["score_components"]


def test_disabled_library_never_enters_local_candidates_or_trace(app, tmp_path):
    root = tmp_path / "disabled-library"
    root.mkdir()
    _candidate(app, root, "disabled", 99, "07-28")
    with app.extensions["inktime_database"].session() as connection:
        library_id = connection.execute("SELECT library_id FROM photos WHERE id='disabled'").fetchone()[0]
        connection.execute("UPDATE libraries SET enabled=0 WHERE id=?", (library_id,))
    policy = LocalSelectionPolicy(
        app.extensions["inktime_database"], app.extensions["inktime_settings_repository"],
        app.extensions["inktime_resilience_repository"],
    )
    result = policy.select(target=date(2026, 7, 28), orientation="portrait", quantity=2, layout="photo_pair")
    assert result["candidates"] == []
    assert result["selected"] == []
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE libraries SET enabled=1 WHERE id=?", (library_id,))
    assert policy.ranked(target=date(2026, 7, 28), orientation="portrait")[0]["id"] == "disabled"


def test_history_fallback_and_leap_day_are_explicit_and_bounded(app, tmp_path):
    root = tmp_path / "history"
    root.mkdir()
    _candidate(app, root, "nearby", 80, "07-27")
    _candidate(app, root, "ranked", 99, "01-01")
    _candidate(app, root, "leap-fallback", 70, "02-28")
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.history_today_window_days", 3, changed_by="test", source_ip="test")
    settings.update("render.history_today_fallback", "nearby_only", changed_by="test", source_ip="test")
    policy = LocalSelectionPolicy(
        app.extensions["inktime_database"], app.extensions["inktime_settings_repository"],
        app.extensions["inktime_resilience_repository"],
    )
    nearby = policy.select(target=date(2026, 7, 28), orientation="portrait", quantity=1, layout="photo_info")
    assert nearby["fallback"] == "nearby_day"
    assert [row["id"] for row in nearby["selected"]] == ["nearby"]
    settings.update("render.history_today_fallback", "none", changed_by="test", source_ip="test")
    none = policy.select(target=date(2026, 7, 28), orientation="portrait", quantity=1, layout="photo_info")
    assert none["selected"] == []
    leap = policy.select(target=date(2025, 2, 28), target_month_day="02-29", orientation="portrait", quantity=1, layout="photo_info")
    assert [row["id"] for row in leap["selected"]] == ["leap-fallback"]
    with app.extensions["inktime_database"].session() as connection:
        trace = connection.execute(
            "SELECT context_snapshot_json FROM selection_decision_traces WHERE trace_id=?", (leap["decision_trace_id"],)
        ).fetchone()
    context = json.loads(trace[0])
    assert context["requested_month_day"] == "02-29"
    assert context["effective_month_day"] == "02-28"
    assert context["fallback_reason"] == "non_leap_year_fallback"


def test_pair_secondary_stays_inside_effective_fallback_pool(app, tmp_path):
    root = tmp_path / "pair-pool"
    root.mkdir()
    _candidate(app, root, "exact-a", 80, "07-28")
    _candidate(app, root, "nearby-b", 70, "07-27")
    _candidate(app, root, "ranked-outside", 100, "01-01")
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.history_today_fallback", "nearby_only", changed_by="test", source_ip="test")
    policy = LocalSelectionPolicy(app.extensions["inktime_database"], settings, app.extensions["inktime_resilience_repository"])
    result = policy.select(target=date(2026, 7, 28), orientation="portrait", quantity=2, layout="photo_pair")
    assert {row["id"] for row in result["selected"][:2]} == {"exact-a", "nearby-b"}
    assert {row["id"] for row in result["allowed_pool"]} == {"exact-a", "nearby-b"}
    assert result["fallback"] == "nearby_day"
    with app.extensions["inktime_database"].session() as connection:
        trace = connection.execute(
            "SELECT context_snapshot_json FROM selection_decision_traces WHERE trace_id=?", (result["decision_trace_id"],)
        ).fetchone()
    context = json.loads(trace[0])
    assert context["secondary_selection_stage"] == "nearby"
    assert context["pair_candidate_count"] == 1


def test_pair_score_uses_only_offline_resolved_city(app, tmp_path):
    root = tmp_path / "location"
    root.mkdir()
    _candidate(app, root, "taipei-a", 80, "07-28")
    _candidate(app, root, "taipei-b", 70, "07-28")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET gps_lat=25.05306,gps_lon=121.52639 WHERE id IN ('taipei-a','taipei-b')")
    policy = LocalSelectionPolicy(
        app.extensions["inktime_database"], app.extensions["inktime_settings_repository"],
        app.extensions["inktime_resilience_repository"], app.extensions["inktime_location_resolver"],
    )
    result = policy.select(target=date(2026, 7, 28), orientation="portrait", quantity=2, layout="photo_pair")
    assert result["selected"][0]["pair_score_components"]["known_location_match"] == 3.0
    assert "gps_lat" not in result["selected"][0]["pair_score_components"]
