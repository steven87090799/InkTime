from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from PIL import Image

from inktime.app.domain.analysis.scoring import LOCAL_QUALITY_SCORE_KIND, SEMANTIC_SCORE_KIND
from inktime.app.domain.photos import PhotoPreprocessor
from inktime.app.workers.scanner import PhotoScanner
from tests.conftest import create_admin, login
from tests.unit.test_analysis_schema import valid_result


def _scan_one(app, root: Path, filename: str = "local-photo.jpg") -> str:
    root.mkdir()
    Image.new("RGB", (1200, 800), "#527f99").save(root / filename)
    photos = app.extensions["inktime_photo_repository"]
    PhotoScanner(
        photos,
        PhotoPreprocessor(),
        app.extensions["inktime_thumbnail_cache"],
    ).scan("local semantic regression", root, build_thumbnails=False)
    with app.extensions["inktime_database"].session() as connection:
        return str(connection.execute("SELECT id FROM photos").fetchone()[0])


def _insert_photo(app, root: Path, photo_id: str, *, local_score: float = 55) -> None:
    root.mkdir(exist_ok=True)
    Image.new("RGB", (640, 480), "#527f99").save(root / f"{photo_id}.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("score source regression", root)
    now = "2026-08-30T00:00:00+00:00"
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            INSERT INTO photos(
                id,library_id,relative_path,status,eligible,lifecycle_status,exclusion_status,
                local_features_status,local_candidate_score,width,height,blur_score,contrast,
                overexposed_ratio,underexposed_ratio,screenshot_likelihood,created_at,updated_at
            ) VALUES (?,?,?,'discovered',1,'active','eligible','complete',?,?,?,?,?,?,?,?,?,?)
            """,
            (
                photo_id,
                library_id,
                f"{photo_id}.jpg",
                local_score,
                640,
                480,
                ((max(36.8, local_score) - 36.8) / 3.2) ** 2,
                40,
                0,
                0,
                0,
                now,
                now,
            ),
        )


def _save_semantic(app, photo_id: str, score: float) -> None:
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE photos SET blur_score=?,contrast=40,width=640,height=480,"
            "overexposed_ratio=0,underexposed_ratio=0 WHERE id=?",
            (((score - 36.8) / 3.2) ** 2, photo_id),
        )
    result = valid_result(
        memory_score=score,
        visual_score=score,
        special_level=0,
        special_codes=[],
    )
    app.extensions["inktime_photo_repository"].save_analysis(
        photo_id,
        None,
        "single",
        "test-ai",
        "test-model",
        result,
        json.dumps(result, ensure_ascii=False),
        score_kind=SEMANTIC_SCORE_KIND,
        ranking_score=score,
        semantic_score=score,
        base_ranking_score=score,
        final_ranking_score=score,
    )


def _save_local(app, photo_id: str, score: float) -> None:
    photos = app.extensions["inktime_photo_repository"]
    result = valid_result(caption="這是一段本機候選品質測試說明文字。")
    photos.save_analysis(
        photo_id,
        None,
        "local_fallback",
        "local",
        "local-quality-v5",
        result,
        "{}",
        score_kind=LOCAL_QUALITY_SCORE_KIND,
        local_score=score,
    )


def _set_capture_date(app, photo_id: str, captured_at: str) -> None:
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            UPDATE photos
            SET captured_at=?,captured_date=?,captured_month_day=?,capture_date_status='valid'
            WHERE id=?
            """,
            (captured_at, captured_at[:10], captured_at[5:10], photo_id),
        )


def test_local_analysis_persists_quality_only_and_no_semantic_ranking(app, tmp_path):
    photo_id = _scan_one(app, tmp_path / "local")
    service = app.extensions["inktime_analysis_service"]

    result = service.analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=None,
        strategy="local",
    )

    analysis = result["analysis"]
    assert analysis["score_kind"] == LOCAL_QUALITY_SCORE_KIND
    assert analysis["semantic_scores_available"] is False
    assert analysis["local_score"] is not None
    assert analysis["memory_score"] is not None
    assert analysis["visual_score"] is not None
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            """
            SELECT score_kind,local_score,memory_score,visual_score,semantic_score,
                   base_ranking_score,final_ranking_score,ranking_score,raw_json,semantic_json
            FROM photo_analysis WHERE photo_id=? ORDER BY id DESC LIMIT 1
            """,
            (photo_id,),
        ).fetchone()
        photo = connection.execute(
            "SELECT local_candidate_score FROM photos WHERE id=?", (photo_id,)
        ).fetchone()
    assert row["score_kind"] == LOCAL_QUALITY_SCORE_KIND
    assert row["local_score"] == photo["local_candidate_score"]
    assert all(row[field] is None for field in (
        "memory_score",
        "visual_score",
        "semantic_score",
        "base_ranking_score",
        "final_ranking_score",
        "ranking_score",
    ))
    semantic = json.loads(row["semantic_json"])
    assert semantic["score_kind"] == LOCAL_QUALITY_SCORE_KIND
    assert semantic["semantic_scores_available"] is False
    assert semantic["values"] == {}


def test_score_population_excludes_current_and_historical_local_ranking_rows(app, tmp_path):
    root = tmp_path / "population"
    for photo_id, score in (("ai-70", 70), ("ai-80", 80), ("local-1", 55), ("local-2", 55), ("local-3", 55)):
        _insert_photo(app, root, photo_id)
        if photo_id.startswith("ai"):
            _save_semantic(app, photo_id, score)
        else:
            photos = app.extensions["inktime_photo_repository"]
            photos.save_analysis(
                photo_id,
                None,
                "local",
                "local",
                "local-quality-v5",
                valid_result(),
                "{}",
                score_kind=LOCAL_QUALITY_SCORE_KIND,
            )
            # Simulate the pre-fix historical local row that already contains
            # an invented semantic ranking.  Its source remains explicit local.
            with app.extensions["inktime_database"].session() as connection:
                connection.execute(
                    "UPDATE photo_analysis SET ranking_score=55 WHERE photo_id=?",
                    (photo_id,),
                )

    population = app.extensions["inktime_photo_repository"].score_population()
    assert sorted(population) == [70.0, 80.0]


def test_semantic_population_cache_invalidates_after_ai_write(app, tmp_path):
    root = tmp_path / "population-cache"
    _insert_photo(app, root, "ai-70")
    _save_semantic(app, "ai-70", 70)
    _insert_photo(app, root, "ai-80")
    _save_semantic(app, "ai-80", 80)

    repository = app.extensions["inktime_photo_repository"]
    assert sorted(repository.score_population()) == [70.0, 80.0]

    _insert_photo(app, root, "ai-90")
    _save_semantic(app, "ai-90", 90)
    assert sorted(repository.score_population()) == [70.0, 80.0, 90.0]


def test_manual_update_preserves_intrinsic_v4_ranking(app, tmp_path):
    root = tmp_path / "manual-ranking"
    _insert_photo(app, root, "manual-ranking", local_score=80)
    _save_semantic(app, "manual-ranking", 80)
    user_id = create_admin(app)

    app.extensions["inktime_photo_repository"].update_manual(
        "manual-ranking",
        favorite=False,
        captured_at=None,
        types=[],
        side_caption="更新說明",
        changed_by=user_id,
    )

    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            """
            SELECT base_ranking_score,effective_special_level,final_ranking_score,ranking_score
            FROM photo_analysis WHERE photo_id=? ORDER BY id DESC LIMIT 1
            """,
            ("manual-ranking",),
        ).fetchone()
    assert tuple(row) == (80.0, 0, 80.0, 80.0)


def test_manual_favorite_update_uses_v4_special_level(app, tmp_path):
    root = tmp_path / "manual-favorite"
    _insert_photo(app, root, "manual-favorite", local_score=80)
    _save_semantic(app, "manual-favorite", 80)
    user_id = create_admin(app)

    app.extensions["inktime_photo_repository"].update_manual(
        "manual-favorite",
        favorite=True,
        captured_at=None,
        types=[],
        side_caption="標記收藏",
        changed_by=user_id,
    )

    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            """
            SELECT base_ranking_score,effective_special_level,final_ranking_score,ranking_score
            FROM photo_analysis WHERE photo_id=? ORDER BY id DESC LIMIT 1
            """,
            ("manual-favorite",),
        ).fetchone()
    assert tuple(row) == (80.0, 1, 82.0, 82.0)


def test_local_manual_update_keeps_semantic_ranking_null(app, tmp_path):
    photo_id = _scan_one(app, tmp_path / "local-manual")
    app.extensions["inktime_analysis_service"].analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=None,
        strategy="local",
    )
    user_id = create_admin(app)

    app.extensions["inktime_photo_repository"].update_manual(
        photo_id,
        favorite=True,
        captured_at=None,
        types=["日常"],
        side_caption="本機更新",
        changed_by=user_id,
    )

    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT score_kind,semantic_score,base_ranking_score,final_ranking_score,ranking_score FROM photo_analysis WHERE photo_id=? ORDER BY id DESC LIMIT 1",
            (photo_id,),
        ).fetchone()
    assert row["score_kind"] == LOCAL_QUALITY_SCORE_KIND
    assert all(row[field] is None for field in ("semantic_score", "base_ranking_score", "final_ranking_score", "ranking_score"))


def test_semantic_selection_survives_newer_local_history(app, tmp_path):
    root = tmp_path / "mixed-source"
    _insert_photo(app, root, "mixed-source", local_score=82)
    _save_semantic(app, "mixed-source", 78)
    app.extensions["inktime_photo_repository"].save_analysis(
        "mixed-source",
        None,
        "local_fallback",
        "local",
        "local-quality-v3",
        valid_result(caption="這是一段本機備援結果測試說明文字。"),
        "{}",
        score_kind=LOCAL_QUALITY_SCORE_KIND,
        ranking_score=55,
    )

    candidates = app.extensions["inktime_render_candidate_repository"]
    preferred = candidates.get("mixed-source")
    assert preferred is not None
    assert preferred["score_kind"] == SEMANTIC_SCORE_KIND
    assert preferred["ranking_score"] == 78

    selected = app.extensions["inktime_render_service"]._candidate_query(
        target=date(2026, 8, 30),
        month_days=None,
        older_only=False,
        limit=1,
        score_kind=SEMANTIC_SCORE_KIND,
    )
    assert [row["id"] for row in selected] == ["mixed-source"]
    assert app.extensions["inktime_photo_repository"].score_population() == [78.0]


def test_local_detail_does_not_render_compatibility_values_as_ai_scores(client, app, tmp_path):
    photo_id = _scan_one(app, tmp_path / "detail")
    app.extensions["inktime_analysis_service"].analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=None,
        strategy="local",
    )
    create_admin(app)
    login(client)

    response = client.get(f"/photos/{photo_id}")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "本機影像分析" in body
    assert "本機候選品質分" in body
    assert "AI 語意評分</dt><dd>尚未進行" in body
    assert "照片庫鑑別分</dt><dd>尚未產生" in body
    assert "原始綜合排序</dt><dd>尚未產生" in body
    assert "語意四項分數" not in body
    assert "回憶 50｜美觀 50｜技術 50｜情緒 50" not in body
    assert "此結果僅為本機影像品質分析，不等同 AI" in body

    listing = client.get("/photos")
    listing_body = listing.get_data(as_text=True)
    assert listing.status_code == 200
    assert "本機候選品質" in listing_body
    assert "AI 語意評分：尚未分析" in listing_body
    assert "照片庫鑑別分：尚未產生" in listing_body
    assert "AI 原始綜合" not in listing_body
    assert "語意四項分數" not in listing_body

    filtered = client.get("/photos?score=70")
    assert filtered.status_code == 200
    assert photo_id not in filtered.get_data(as_text=True)


def test_local_only_and_ai_disabled_use_local_selection_without_semantic_rank(app, tmp_path):
    photo_id = _scan_one(app, tmp_path / "local-selection")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            UPDATE photos
            SET blur_score=1000,contrast=40,overexposed_ratio=0,underexposed_ratio=0,
                screenshot_likelihood=0,e6_score=80,local_features_status='complete',
                local_candidate_score=82.7,eligible=1,exclusion_status='eligible'
            WHERE id=?
            """,
            (photo_id,),
        )
    app.extensions["inktime_analysis_service"].analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=None,
        strategy="local",
    )
    settings = app.extensions["inktime_settings_repository"]
    render_service = app.extensions["inktime_render_service"]
    for mode in ("local_only", "disabled"):
        settings.update("analysis.execution_mode", mode, changed_by="test", source_ip="127.0.0.1")
        assert render_service.select_candidates(quantity=1) == [photo_id]
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT score_kind,ranking_score,semantic_score,local_score FROM photo_analysis WHERE photo_id=?",
            (photo_id,),
        ).fetchone()
    assert row["score_kind"] == LOCAL_QUALITY_SCORE_KIND
    assert row["ranking_score"] is None
    assert row["semantic_score"] is None
    assert row["local_score"] is not None


def test_automatic_ai_selects_semantic_then_local_fallback_by_tier(app, tmp_path):
    settings = app.extensions["inktime_settings_repository"]
    settings.update("analysis.execution_mode", "automatic_ai", changed_by="test", source_ip="test")
    settings.update("render.selection_mode", "top_ranked", changed_by="test", source_ip="test")
    root = tmp_path / "tiered-selection"
    for index in range(1, 4):
        photo_id = f"tier-semantic-{index}"
        _insert_photo(app, root, photo_id, local_score=40)
        _save_semantic(app, photo_id, 80 - index)
    for index in range(1, 11):
        photo_id = f"tier-local-{index}"
        _insert_photo(app, root, photo_id, local_score=99 - index)
        _save_local(app, photo_id, 99 - index)

    selected = app.extensions["inktime_render_service"].select_candidates_details(5)

    assert len(selected) == 5
    assert [row["score_kind"] for row in selected[:3]] == [SEMANTIC_SCORE_KIND] * 3
    assert [row["score_kind"] for row in selected[3:]] == [LOCAL_QUALITY_SCORE_KIND] * 2


def test_history_today_uses_local_fallback_to_fill_semantic_shortage(app, tmp_path):
    settings = app.extensions["inktime_settings_repository"]
    settings.update("analysis.execution_mode", "automatic_ai", changed_by="test", source_ip="test")
    root = tmp_path / "history-tiered-selection"
    _insert_photo(app, root, "history-semantic", local_score=40)
    _set_capture_date(app, "history-semantic", "2020-07-20T10:00:00")
    _save_semantic(app, "history-semantic", 80)
    for index in range(1, 4):
        photo_id = f"history-local-{index}"
        _insert_photo(app, root, photo_id, local_score=90 - index)
        _set_capture_date(app, photo_id, "2020-07-20T10:00:00")
        _save_local(app, photo_id, 90 - index)

    selected = app.extensions["inktime_render_service"].select_candidates_details(
        4, target_date=date(2026, 7, 20)
    )

    assert len(selected) == 4
    assert [row["score_kind"] for row in selected] == [
        SEMANTIC_SCORE_KIND,
        LOCAL_QUALITY_SCORE_KIND,
        LOCAL_QUALITY_SCORE_KIND,
        LOCAL_QUALITY_SCORE_KIND,
    ]
    assert [row["match_type"] for row in selected] == ["exact_day"] * 4


def test_automatic_ai_never_compares_local_score_with_semantic_score(app, tmp_path):
    settings = app.extensions["inktime_settings_repository"]
    settings.update("analysis.execution_mode", "automatic_ai", changed_by="test", source_ip="test")
    settings.update("render.selection_mode", "top_ranked", changed_by="test", source_ip="test")
    settings.update("render.memory_threshold", 0, changed_by="test", source_ip="test")
    root = tmp_path / "tiered-score-domain"
    _insert_photo(app, root, "semantic-60", local_score=20)
    _save_semantic(app, "semantic-60", 60)
    _insert_photo(app, root, "local-99", local_score=99)
    _save_local(app, "local-99", 99)

    selected = app.extensions["inktime_render_service"].select_candidates_details(1)

    assert [row["id"] for row in selected] == ["semantic-60"]
    assert selected[0]["score_kind"] == SEMANTIC_SCORE_KIND


def test_semantic_detail_retains_v4_ranking_percentile_and_components(client, app, tmp_path):
    root = tmp_path / "semantic-detail"
    _insert_photo(app, root, "semantic-detail")
    _save_semantic(app, "semantic-detail", 78)
    for score in (70, 71, 72, 73, 74):
        peer_id = f"semantic-peer-{score}"
        _insert_photo(app, root, peer_id)
        _save_semantic(app, peer_id, score)
    create_admin(app)
    login(client)

    response = client.get("/photos/semantic-detail")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "模型判斷" in body
    assert "照片庫鑑別分" in body
    assert "原始綜合排序</dt><dd>78.0" in body or "原始綜合排序</dt><dd>78" in body
    assert "排序組成" in body
    assert "回憶 78" in body
    assert "視覺 78" in body
    assert "本機品質 78" in body
    assert "已完成 AI 語意評分照片" in body
