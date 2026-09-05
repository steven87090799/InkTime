from __future__ import annotations

from PIL import Image

from inktime.app.domain.analysis.scoring import SEMANTIC_SCORE_KIND
from tests.conftest import create_admin, csrf, login
from tests.unit.test_analysis_schema import valid_result


def _candidate(app, root, photo_id: str, *, eligible: int = 1, lifecycle: str = "active"):
    Image.new("RGB", (640, 480), "white").save(root / f"{photo_id}.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library = photos.ensure_library("候選契約", root)
    now = "2026-07-22T00:00:00+00:00"
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            INSERT INTO photos(id,library_id,relative_path,status,eligible,lifecycle_status,
                               local_features_status,captured_at,created_at,updated_at)
            VALUES (?,?,?,'analyzed',?,?,'complete', '2020-07-22T00:00:00',?,?)
            """,
            (photo_id, library, f"{photo_id}.jpg", eligible, lifecycle, now, now),
        )
    photos.save_analysis(
        photo_id,
        None,
        "test-model",
        "local",
        "test",
        valid_result(
            caption="這是一段候選契約測試說明。",
            types=["其他"],
            memory_score=99,
            visual_score=99,
            side_caption="這是一段測試回憶短句。",
        ),
        "{}",
        score_kind=SEMANTIC_SCORE_KIND,
        ranking_score=99,
        final_ranking_score=99,
    )


def test_manual_excluded_photo_returns_stable_error_without_fallback(client, app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _candidate(app, root, "excluded-high", eligible=0)
    _candidate(app, root, "eligible-lower")
    create_admin(app)
    login(client)
    response = client.post(
        "/api/v1/releases",
        json={"photo_ids": ["excluded-high"]},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 409
    assert response.get_json()["error_code"] == "RENDER-009"


def test_missing_or_removed_file_is_never_a_candidate(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _candidate(app, root, "removed")
    (root / "removed.jpg").unlink()
    assert app.extensions["inktime_render_candidate_repository"].get("removed") is None


def test_local_only_explicit_local_feature_photo_can_queue_release(client, app, tmp_path):
    root = tmp_path / "local-only"
    root.mkdir()
    Image.new("RGB", (640, 480), "white").save(root / "local.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library = photos.ensure_library("本機", root)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """INSERT INTO photos(id,library_id,relative_path,status,eligible,lifecycle_status,
               exclusion_status,local_features_status,created_at,updated_at)
               VALUES ('local-only',?,'local.jpg','discovered',1,'active','eligible','complete','2026-01-01','2026-01-01')""",
            (library,),
        )
    create_admin(app)
    login(client)
    response = client.post(
        "/api/v1/releases", json={"photo_ids": ["local-only"]}, headers={"X-CSRF-Token": csrf(client)}
    )
    assert response.status_code == 202


def test_non_automatic_modes_mix_analysis_and_local_contracts(app, tmp_path):
    root = tmp_path / "mixed"
    root.mkdir()
    _candidate(app, root, "analysed-old")
    Image.new("RGB", (640, 480), "white").save(root / "scanner.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library = photos.ensure_library("混合資格", root)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """INSERT INTO photos(id,library_id,relative_path,status,eligible,lifecycle_status,
               exclusion_status,local_features_status,created_at,updated_at)
               VALUES ('scanner-local',?,'scanner.jpg','discovered',1,'active','eligible','complete','2026-01-01','2026-01-01')""",
            (library,),
        )
    candidates = app.extensions["inktime_render_candidate_repository"]
    for mode in ("local_only", "local_with_manual_ai", "disabled"):
        rows = candidates.require_for_execution_mode(["analysed-old", "scanner-local", "analysed-old"], mode)
        assert [row["id"] for row in rows] == ["analysed-old", "scanner-local"]
        assert [row["eligibility_source"] for row in rows] == ["analysis", "local"]
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.layout", "photo_pair", changed_by="test", source_ip="test")
    release = app.extensions["inktime_render_service"].publish(["analysed-old", "scanner-local"], "test")
    manifest = app.extensions["inktime_release_publisher"].validate(release["release_id"])
    plan = manifest["render_options"]["render_plans"][0]
    assert plan["primary_eligibility_source"] == "analysis"
    assert plan["secondary_eligibility_source"] == "local"
    try:
        candidates.require_for_execution_mode(["analysed-old", "scanner-local"], "automatic_ai")
    except ValueError as exc:
        assert "scanner-local" in str(exc)
        assert "正式分析" in str(exc)
    else:
        raise AssertionError("automatic_ai must not accept a local-only candidate")


def test_automatic_local_selection_records_per_photo_eligibility_sources(app, tmp_path):
    root = tmp_path / "automatic-local"
    root.mkdir()
    _candidate(app, root, "automatic-analysis")
    Image.new("RGB", (900, 1600), "#6688aa").save(root / "automatic-local.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library = photos.ensure_library("自動本機資格", root)
    now = "2026-07-28T10:00:00+00:00"
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """UPDATE photos SET width=900,height=1600,eligible=1,lifecycle_status='active',
               exclusion_status='eligible',local_features_status='complete',local_candidate_score=95,
               captured_at='2020-07-28T10:00:00+00:00',captured_date='2020-07-28',captured_month_day='07-28'
               WHERE id='automatic-analysis'"""
        )
        connection.execute(
            """INSERT INTO photos(id,library_id,relative_path,width,height,status,eligible,lifecycle_status,
               exclusion_status,local_features_status,local_candidate_score,captured_at,captured_date,
               captured_month_day,created_at,updated_at)
               VALUES ('automatic-local',?,'automatic-local.jpg',900,1600,'discovered',1,'active',
               'eligible','complete',80,'2020-07-28T10:00:00+00:00','2020-07-28','07-28',?,?)""",
            (library, now, now),
        )
    settings = app.extensions["inktime_settings_repository"]
    settings.update("analysis.execution_mode", "local_only", changed_by="test", source_ip="test")
    settings.update("render.layout", "photo_pair", changed_by="test", source_ip="test")
    settings.update("render.quantity", 1, changed_by="test", source_ip="test")
    release = app.extensions["inktime_render_service"].publish([], "test")
    manifest = app.extensions["inktime_release_publisher"].validate(release["release_id"])
    plan = manifest["render_options"]["render_plans"][0]
    assert plan["primary_eligibility_source"] == "analysis"
    assert plan["secondary_eligibility_source"] == "local"


def test_automatic_ai_revalidates_explicit_local_only_selection(app, tmp_path):
    root = tmp_path / "automatic-ai"
    root.mkdir()
    Image.new("RGB", (640, 480), "white").save(root / "local.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library = photos.ensure_library("自動 AI 資格", root)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """INSERT INTO photos(id,library_id,relative_path,status,eligible,lifecycle_status,
               exclusion_status,local_features_status,created_at,updated_at)
               VALUES ('automatic-local-only',?,'local.jpg','discovered',1,'active','eligible','complete',
               '2026-01-01','2026-01-01')""",
            (library,),
        )
    settings = app.extensions["inktime_settings_repository"]
    settings.update("analysis.execution_mode", "automatic_ai", changed_by="test", source_ip="test")
    service = app.extensions["inktime_render_service"]
    service.select_candidates = lambda _limit: ["automatic-local-only"]
    try:
        service.publish(["automatic-local-only"], "test")
    except ValueError as exc:
        assert "automatic-local-only" in str(exc)
        assert "正式分析" in str(exc)
    else:
        raise AssertionError("automatic_ai must revalidate automatic local-only selection")


def test_candidate_error_names_the_actual_photo(app, tmp_path):
    root = tmp_path / "errors"
    root.mkdir()
    _candidate(app, root, "disabled-library")
    with app.extensions["inktime_database"].session() as connection:
        library_id = connection.execute(
            "SELECT library_id FROM photos WHERE id='disabled-library'"
        ).fetchone()[0]
        connection.execute("UPDATE libraries SET enabled=0 WHERE id=?", (library_id,))
    candidates = app.extensions["inktime_render_candidate_repository"]
    for photo_id in ("does-not-exist", "disabled-library"):
        try:
            candidates.require_for_execution_mode([photo_id], "local_only")
        except ValueError as exc:
            assert photo_id in str(exc)
        else:
            raise AssertionError("ineligible candidate must fail")
