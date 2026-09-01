from __future__ import annotations

import json
from datetime import date
import pytest
from PIL import Image

from tests.conftest import create_admin, login
from tests.integration.test_photo_quality_ai import CountingProvider, _scan
from tests.integration.test_jobs import add_photos
from tests.unit.test_analysis_schema import valid_result
from inktime.app.domain.analysis.scoring import calculate_distinguishing_score
from inktime.app.domain.analysis.content_filter import CONTENT_FILTER_SWITCHES
from inktime.app.domain.photos import PhotoPreprocessor
from inktime.app.domain.photos.quality_policy import evaluate_local_quality, local_candidate_score
from inktime.app.workers.scanner import PhotoScanner


def save(repo, photo_id, **updates):
    result = valid_result(**updates)
    repo.save_analysis(
        photo_id, None, "single", "mock", "mock", result, json.dumps(result, ensure_ascii=False)
    )
    return result


def test_defaults_and_settings_ui(app, client):
    create_admin(app)
    login(client)
    settings = app.extensions["inktime_settings_repository"]
    for key in CONTENT_FILTER_SWITCHES.values():
        assert settings.get(key) is True
    assert settings.get("analysis.content_filter_min_confidence") == 0.85
    assert settings.get("analysis.female_glamour_min_confidence") == 0.9
    assert [settings.get(f"analysis.caption_{x}_chars") for x in ("min", "target", "max")] == [30, 60, 100]
    assert settings.get("render.e6_weight") == 20
    response = client.get("/settings?mode=all")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    for label in (
        "排除偏色情／性感照片",
        "排除成人裸露照片",
        "排除女性單人寫真",
        "內容排除最低信心",
        "女性寫真最低信心",
    ):
        assert label in text
    assert client.get("/scoring").status_code == 200


@pytest.mark.parametrize("code", list(CONTENT_FILTER_SWITCHES))
@pytest.mark.parametrize(
    "confidence,enabled,excluded", [(0.95, True, True), (0.8, True, False), (0.95, False, False)]
)
def test_authoritative_exclusion_retains_analysis_orientation(app, code, confidence, enabled, excluded):
    actor = create_admin(app)
    settings = app.extensions["inktime_settings_repository"]
    settings.update(CONTENT_FILTER_SWITCHES[code], enabled, changed_by=actor, source_ip="test")
    repo = app.extensions["inktime_photo_repository"]
    photo_id = add_photos(app, 1)[0]
    result = save(repo, photo_id, content_filter={"exclude_code": code, "confidence": confidence})
    photo = repo.get_with_path(photo_id)
    assert bool(photo["eligible"]) is not excluded
    assert (photo["exclusion_status"] == "auto_excluded") == excluded
    assert photo["visual_orientation_rotation_cw"] == 0
    if excluded:
        assert photo["reject_reason"] == code
        assert result["selection_score"] == 0
        assert repo.score_population() == []
    else:
        assert repo.score_population() == [result["ranking_score"]]
    with repo.database.session() as connection:
        row = connection.execute("SELECT * FROM photo_analysis WHERE photo_id=?", (photo_id,)).fetchone()
        assert (
            row["schema_version"] == 4
            and json.loads(row["raw_json"])["content_filter"]["exclude_code"] == code
        )
        assert (
            row["reason"] is None and row["technical_quality_score"] is None and row["emotion_score"] is None
        )


def test_restore_rescan_reanalysis_and_explicit_reapply(app, tmp_path):
    actor = create_admin(app)
    photo_id = _scan(app, tmp_path)[0]
    repo = app.extensions["inktime_photo_repository"]
    content = {"exclude_code": "female_glamour_portrait", "confidence": 0.95}
    save(repo, photo_id, content_filter=content)
    repo.set_exclusion(photo_id, action="restore", changed_by=actor)
    # Scan changed bytes too: only explicit reapply may reset a manual restore.
    Image.effect_noise((900, 600), 80).convert("RGB").save(tmp_path / "photos" / "photo.jpg")
    PhotoScanner(repo, PhotoPreprocessor(), app.extensions["inktime_thumbnail_cache"]).scan(
        "測試照片", tmp_path / "photos", build_thumbnails=False
    )
    save(repo, photo_id, content_filter=content)
    assert repo.get_with_path(photo_id)["exclusion_status"] == "manually_restored"
    repo.set_exclusion(photo_id, action="reanalyze", changed_by=actor)
    assert repo.get_with_path(photo_id)["eligible"] == 1
    repo.set_exclusion(photo_id, action="reanalyze", changed_by=actor, reapply_rules=True)
    photo = repo.get_with_path(photo_id)
    assert (
        photo["exclusion_status"] == "auto_excluded" and photo["reject_reason"] == "female_glamour_portrait"
    )
    assert photo["reject_rule"] == "content-filter"


def test_local_quality_favorite_population_and_caption_search(app):
    actor = create_admin(app)
    repo = app.extensions["inktime_photo_repository"]
    ids = add_photos(app, 6)
    with repo.database.session() as connection:
        connection.execute(
            "UPDATE photos SET blur_score=144,contrast=30,width=1800,height=1200,overexposed_ratio=.1,underexposed_ratio=.05,e6_score=90"
        )
    results = [
        save(repo, photo_id, memory_score=50 + i * 8, special_level=0) for i, photo_id in enumerate(ids)
    ]
    quality = local_candidate_score(
        repo.get_with_path(ids[0]), evaluation=evaluate_local_quality(repo.get_with_path(ids[0]))
    )
    assert results[0]["local_quality_score"] == quality
    assert results[0]["ranking_score"] == round(50 * 0.5 + 81 * 0.25 + quality * 0.25, 2)
    old_score = results[0]["ranking_score"]
    repo.update_manual(
        ids[0],
        favorite=True,
        captured_at=None,
        types=["人物"],
        side_caption="釣竿伸向雲層深處。",
        changed_by=actor,
    )
    with repo.database.session() as connection:
        row = connection.execute(
            "SELECT ranking_score,final_ranking_score,effective_special_level FROM photo_analysis WHERE photo_id=?",
            (ids[0],),
        ).fetchone()
        assert (
            row["ranking_score"] == round(old_score + 2, 2) == row["final_ranking_score"]
            and row["effective_special_level"] == 1
        )
    save(repo, ids[-1], content_filter={"exclude_code": "sexualized_content", "confidence": 0.99})
    assert len(repo.score_population()) == 5
    rows, count = repo.search(query="釣具")
    assert count == 6 and len(rows) == 6  # excluded analysis remains searchable


def test_real_service_uses_v4_and_content_policy(app, tmp_path):
    photo_id = _scan(app, tmp_path)[0]
    provider = CountingProvider(
        valid_result(content_filter={"exclude_code": "explicit_nudity", "confidence": 0.95})
    )
    actor = create_admin(app)
    app.extensions["inktime_settings_repository"].update(
        "analysis.execution_mode", "automatic_ai", changed_by=actor, source_ip="test"
    )
    service = app.extensions["inktime_analysis_service"]
    result = service.analyze_photo(
        photo_id=photo_id, job_id=None, provider=provider, strategy="single", force_ai=True
    )
    assert provider.analyze_calls == 1
    assert result["analysis"]["schema_version"] == 4
    assert (
        app.extensions["inktime_photo_repository"].get_with_path(photo_id)["reject_reason"]
        == "explicit_nudity"
    )


def test_render_percentile_precedes_e6_and_excluded_is_never_selected(app, tmp_path):
    repo = app.extensions["inktime_photo_repository"]
    ids = add_photos(app, 6)
    for i in range(6):
        (tmp_path / f"{i}.jpg").write_bytes(b"only-file-presence-required")
    with repo.database.session() as connection:
        connection.execute("UPDATE libraries SET root_path=?", (str(tmp_path),))
        connection.execute(
            "UPDATE photos SET eligible=1,local_features_status='complete',crop_focus_x=.5,crop_focus_y=.5,e6_score=90"
        )
    for i, photo_id in enumerate(ids):
        save(repo, photo_id, memory_score=70 + i, special_level=0)
    save(repo, ids[-1], content_filter={"exclude_code": "female_glamour_portrait", "confidence": 0.95})
    render = app.extensions["inktime_render_service"]
    rows = render._candidate_query(target=date(2026, 9, 1), month_days=None, older_only=False, limit=10)
    assert len(rows) == 5 and ids[-1] not in {row["id"] for row in rows}
    population = repo.score_population()
    for row in rows:
        distinguishing, percentile = calculate_distinguishing_score(row["ranking_score"], population)
        assert row["ranking_percentile"] == percentile
        assert row["distinguishing_score"] == distinguishing
        assert row["display_score"] == round(distinguishing * 0.8 + 90 * 0.2, 2)


def test_reapply_uses_current_switch_and_duplicate_inheritance_respects_favorite(app):
    actor = create_admin(app)
    repo = app.extensions["inktime_photo_repository"]
    ids = add_photos(app, 2)
    with repo.database.session() as connection:
        connection.execute("UPDATE photos SET sha256='same-bytes'")
        connection.execute("UPDATE photos SET favorite=1 WHERE id=?", (ids[1],))
    save(
        repo,
        ids[0],
        content_filter={"exclude_code": "female_glamour_portrait", "confidence": 0.95},
        special_level=2,
    )
    inherited = repo.inherit_existing_analysis(ids[1], None)
    assert inherited["content_filter"]["exclude_code"] == "female_glamour_portrait"
    assert inherited["special_level"] == 2 and inherited["effective_special_level"] == 3
    assert repo.get_with_path(ids[1])["eligible"] == 1
    app.extensions["inktime_settings_repository"].update(
        "analysis.exclude_female_glamour_portraits", False, changed_by=actor, source_ip="test"
    )
    repo.set_exclusion(ids[0], action="reanalyze", reapply_rules=True, changed_by=actor)
    assert repo.get_with_path(ids[0])["eligible"] == 1


def test_rarity_saved_from_same_library_only_and_excludes_rejected_peers(app):
    repo = app.extensions["inktime_photo_repository"]
    ids = add_photos(app, 21)
    for photo_id in ids[:-1]:
        save(repo, photo_id, types=["風景"], people_count=0, special_codes=[], special_level=0)
    result = save(repo, ids[-1], types=["活動"], people_count=20, special_codes=["ceremony"], special_level=2)
    assert result["library_rarity_adjustment"] == 1 and result["effective_special_level"] == 3
    other = add_photos(app, 1)[0]
    result = save(repo, other, types=["活動"], people_count=20, special_codes=["ceremony"], special_level=2)
    assert result["library_rarity_adjustment"] == 0


def test_server_quality_is_independent_of_e6(app):
    repo = app.extensions["inktime_photo_repository"]
    photo_id = add_photos(app, 1)[0]
    with repo.database.session() as connection:
        connection.execute("UPDATE photos SET blur_score=200,contrast=30,width=1800,height=1200,e6_score=95")
    first = save(repo, photo_id)
    with repo.database.session() as connection:
        connection.execute("UPDATE photos SET e6_score=5")
    second = save(repo, photo_id)
    assert first["local_quality_score"] == second["local_quality_score"]
    assert first["ranking_score"] == second["ranking_score"]


def test_rarity_does_not_depend_on_analysis_arrival_order(app):
    repo = app.extensions["inktime_photo_repository"]
    libraries = [add_photos(app, 21), add_photos(app, 21)]
    rare_ids = []
    for ids, rare_index in zip(libraries, [0, 20], strict=True):
        rare_ids.append(ids[rare_index])
        for index, photo_id in enumerate(ids):
            rare = index == rare_index
            save(
                repo,
                photo_id,
                types=["活動"] if rare else ["風景"],
                special_codes=["ceremony"] if rare else [],
                people_count=20 if rare else 0,
                special_level=2,
            )
    with repo.database.session() as connection:
        rows = connection.execute(
            "SELECT library_rarity_adjustment,ranking_score FROM photo_analysis WHERE photo_id IN (?,?)",
            rare_ids,
        ).fetchall()
    assert [row["library_rarity_adjustment"] for row in rows] == [1, 1]
    assert rows[0]["ranking_score"] == rows[1]["ranking_score"]
    assert len(repo.score_population(repo.get_with_path(rare_ids[0])["library_id"])) == 21
