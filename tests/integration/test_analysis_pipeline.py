from __future__ import annotations

import json
import sqlite3
from PIL import Image

import pytest

from inktime.app.domain.photos import PhotoPreprocessor, ThumbnailCache
from inktime.app.providers.base import ProviderResponse, Usage, VisionProvider
from inktime.app.providers.router import FailoverVisionProvider, ProviderChannel
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.repositories.usage import UsageRepository
from inktime.app.services.analysis import PhotoAnalysisService
from inktime.app.domain.analysis.plan import fingerprint
from inktime.app.workers.scanner import PhotoScanner
from tests.conftest import create_admin
from tests.unit.test_analysis_schema import valid_result


class MockProvider(VisionProvider):
    name = "Mock Provider"

    def __init__(self, responses):
        self.responses = list(responses)
        self.analyze_calls = 0
        self.repair_calls = 0

    def analyze(self, **kwargs):
        self.analyze_calls += 1
        value = self.responses.pop(0)
        return ProviderResponse(
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), Usage(1000, 100, 0)
        )

    def repair_json(self, **kwargs):
        self.repair_calls += 1
        value = self.responses.pop(0)
        return ProviderResponse(
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), Usage(200, 100, 0)
        )

    def submit_batch(self, requests, completion_window="24h"):
        return "batch"

    def poll_batch(self, batch_id):
        return {"status": "completed"}

    def cancel_batch(self, batch_id):
        return {"status": "cancelled"}

    def estimate_cost(self, model, usage):
        return (usage.input_tokens + usage.output_tokens) / 1_000_000

    def validate_config(self):
        return True, "ok"


class FailingProvider(MockProvider):
    name = "Failing Provider"

    def analyze(self, **kwargs):
        self.analyze_calls += 1
        raise RuntimeError("provider unavailable")


def prepare(app, tmp_path, duplicate=False):
    root = tmp_path / "photos"
    root.mkdir()
    Image.new("RGB", (900, 600), (70, 120, 180)).save(root / "a.jpg")
    if duplicate:
        (root / "b.jpg").write_bytes((root / "a.jpg").read_bytes())
    photos = PhotoRepository(app.extensions["inktime_database"])
    cache = ThumbnailCache(tmp_path / "cache")
    result = PhotoScanner(photos, PhotoPreprocessor(), cache).scan("照片", root)
    with app.extensions["inktime_database"].session() as connection:
        ids = [row[0] for row in connection.execute("SELECT id FROM photos ORDER BY relative_path")]
    service = PhotoAnalysisService(photos, UsageRepository(app.extensions["inktime_database"]), cache)
    return result, ids, service


def test_single_model_call_returns_all_fields_and_usage(app, tmp_path):
    _, ids, service = prepare(app, tmp_path)
    provider = MockProvider([valid_result()])
    result = service.analyze_photo(
        photo_id=ids[0], job_id=None, provider=provider, strategy="high_quality", high_model="mock"
    )
    assert provider.analyze_calls == 1
    assert provider.repair_calls == 0
    assert result["analysis"]["side_caption"]
    with app.extensions["inktime_database"].session() as connection:
        usage = connection.execute("SELECT input_tokens,output_tokens FROM api_usage").fetchone()
    assert tuple(usage) == (1000, 100)


def test_provider_and_local_results_persist_a_complete_analysis_context(app, tmp_path):
    _, ids, service = prepare(app, tmp_path)
    service.analyze_photo(
        photo_id=ids[0], job_id=None, provider=MockProvider([valid_result()]), strategy="high_quality"
    )
    (tmp_path / "local").mkdir()
    _, local_ids, local_service = prepare(app, tmp_path / "local")
    local_service.analyze_photo(
        photo_id=local_ids[0], job_id=None, provider=None, strategy="local"
    )
    with app.extensions["inktime_database"].session() as connection:
        rows = connection.execute(
            "SELECT analysis_fingerprint,analysis_spec_json,prompt_version,schema_kind,"
            "scoring_version_id,vision_request_fingerprint,vision_input_spec_json "
            "FROM photo_analysis ORDER BY created_at,id"
        ).fetchall()
    assert len(rows) == 2
    assert all(row["analysis_fingerprint"] and row["analysis_spec_json"] for row in rows)
    assert all(row["prompt_version"] and row["schema_kind"] for row in rows)
    assert all(row["vision_request_fingerprint"] and row["vision_input_spec_json"] for row in rows)


def test_favorite_change_recalculates_latest_ranking_with_original_version(app, tmp_path):
    user_id = create_admin(app)
    _, ids, service = prepare(app, tmp_path)
    profile = app.extensions["inktime_scoring_repository"].current()
    provider = MockProvider([valid_result()])
    service.analyze_photo(
        photo_id=ids[0],
        job_id=None,
        provider=provider,
        strategy="high_quality",
        high_model="mock",
        scoring_version_id=str(profile["id"]),
    )
    repository = app.extensions["inktime_photo_repository"]
    with app.extensions["inktime_database"].session() as connection:
        before = connection.execute(
            "SELECT ranking_score,scoring_version_id FROM photo_analysis WHERE photo_id=?",
            (ids[0],),
        ).fetchone()

    repository.update_manual(
        ids[0],
        favorite=True,
        captured_at=None,
        types=["人物"],
        side_caption="值得收藏的一天",
        changed_by=user_id,
    )

    with app.extensions["inktime_database"].session() as connection:
        after = connection.execute(
            "SELECT ranking_score,scoring_version_id FROM photo_analysis WHERE photo_id=?",
            (ids[0],),
        ).fetchone()
    assert after["ranking_score"] == before["ranking_score"] + profile["favorite_bonus"]
    assert after["scoring_version_id"] == before["scoring_version_id"]


def test_invalid_json_is_repaired_only_once_without_second_image_call(app, tmp_path):
    _, ids, service = prepare(app, tmp_path)
    provider = MockProvider(["not-json", valid_result()])
    service.analyze_photo(
        photo_id=ids[0], job_id=None, provider=provider, strategy="high_quality", high_model="mock"
    )
    assert provider.analyze_calls == 1
    assert provider.repair_calls == 1


def test_smart_stage_filters_low_value_photo(app, tmp_path):
    _, ids, service = prepare(app, tmp_path)
    provider = MockProvider([valid_result(memory_score=40, types=["雜物"])])
    result = service.analyze_photo(
        photo_id=ids[0],
        job_id=None,
        provider=provider,
        strategy="smart_two_stage",
        low_model="cheap",
        high_model="quality",
    )
    assert result["stage"] == "stage_one"
    assert provider.analyze_calls == 1


def test_failover_rebuilds_cache_identity_for_the_next_provider(app, tmp_path):
    _, ids, _service = prepare(app, tmp_path)
    settings = app.extensions["inktime_settings_repository"]
    settings.update("analysis.ai_mode", "eligible", changed_by="test", source_ip="127.0.0.1")
    settings.update("analysis.prefilter_enabled", False, changed_by="test", source_ip="127.0.0.1")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET eligible=1,exclusion_status='eligible' WHERE id=?", (ids[0],))
    failing = FailingProvider([])
    failing.provider_id = "first-provider"
    succeeding = MockProvider([valid_result()])
    succeeding.provider_id = "second-provider"
    router = FailoverVisionProvider(
        [ProviderChannel(failing, priority=1), ProviderChannel(succeeding, priority=2)],
        failure_threshold=1,
    )

    analysis = app.extensions["inktime_analysis_service"]
    plan = analysis.build_plan(
        strategy="high_quality",
        provider_route=[],
        scoring_profile=dict(app.extensions["inktime_scoring_repository"].current()),
    )
    result = analysis.analyze_photo(
        photo_id=ids[0], job_id=None, provider=router, strategy="high_quality", analysis_plan=plan
    )

    assert result["stage"] == "single_high"
    assert failing.analyze_calls == 1
    assert succeeding.analyze_calls == 1
    with app.extensions["inktime_database"].session() as connection:
        cache = connection.execute("SELECT provider FROM ai_analysis_cache").fetchone()
    assert cache["provider"] == "second-provider"


def test_identical_photo_inherits_without_model_call(app, tmp_path):
    scan, ids, service = prepare(app, tmp_path, duplicate=True)
    assert scan["inherited"] == 1
    assert len(ids) == 2
    first = MockProvider([valid_result()])
    service.analyze_photo(
        photo_id=ids[0], job_id=None, provider=first, strategy="high_quality", high_model="mock"
    )
    second = MockProvider([])
    result = service.analyze_photo(
        photo_id=ids[1], job_id=None, provider=second, strategy="high_quality", high_model="mock"
    )
    assert result["stage"] == "inherited"
    assert second.analyze_calls == 0


def test_worker_context_inherits_only_the_same_frozen_plan_and_keeps_source_trace(app, tmp_path):
    _, ids, _service = prepare(app, tmp_path, duplicate=True)
    settings = app.extensions["inktime_settings_repository"]
    settings.update("analysis.ai_mode", "eligible", changed_by="test", source_ip="127.0.0.1")
    settings.update("analysis.prefilter_enabled", False, changed_by="test", source_ip="127.0.0.1")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE photos SET eligible=1,exclusion_status='eligible',manual_override=0 WHERE id IN (?,?)",
            ids,
        )
    service = app.extensions["inktime_analysis_service"]
    plan = service.build_plan(
        strategy="high_quality",
        provider_route=[],
        scoring_profile=dict(app.extensions["inktime_scoring_repository"].current()),
    )
    first = MockProvider([valid_result()])
    service.analyze_photo(
        photo_id=ids[0], job_id=None, provider=first, strategy="high_quality", analysis_plan=plan
    )
    second = MockProvider([])
    inherited = service.analyze_photo(
        photo_id=ids[1], job_id=None, provider=second, strategy="high_quality", analysis_plan=plan
    )
    assert inherited["stage"] == "inherited"
    assert first.analyze_calls == 1
    assert second.analyze_calls == 0
    with app.extensions["inktime_database"].session() as connection:
        source = connection.execute(
            "SELECT id,analysis_fingerprint,vision_request_fingerprint,vision_input_spec_json FROM photo_analysis WHERE photo_id=?",
            (ids[0],),
        ).fetchone()
        copied = connection.execute(
            "SELECT stage,analysis_fingerprint,vision_request_fingerprint,vision_input_spec_json,semantic_json "
            "FROM photo_analysis WHERE photo_id=?",
            (ids[1],),
        ).fetchone()
    assert copied["stage"] == "inherited"
    assert copied["analysis_fingerprint"] == fingerprint(plan) == source["analysis_fingerprint"]
    assert copied["vision_request_fingerprint"] == source["vision_request_fingerprint"]
    assert copied["vision_input_spec_json"] == source["vision_input_spec_json"]
    assert json.loads(copied["semantic_json"])["inherited_from"]["analysis_id"] == source["id"]


def test_worker_context_does_not_inherit_a_different_frozen_plan(app, tmp_path):
    _, ids, _service = prepare(app, tmp_path, duplicate=True)
    settings = app.extensions["inktime_settings_repository"]
    settings.update("analysis.ai_mode", "eligible", changed_by="test", source_ip="127.0.0.1")
    settings.update("analysis.prefilter_enabled", False, changed_by="test", source_ip="127.0.0.1")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE photos SET eligible=1,exclusion_status='eligible',manual_override=0 WHERE id IN (?,?)",
            ids,
        )
    service = app.extensions["inktime_analysis_service"]
    profile = dict(app.extensions["inktime_scoring_repository"].current())
    first_plan = service.build_plan(strategy="high_quality", provider_route=[], scoring_profile=profile)
    first = MockProvider([valid_result()])
    service.analyze_photo(
        photo_id=ids[0], job_id=None, provider=first, strategy="high_quality", analysis_plan=first_plan
    )
    settings.update("model.high_model", "new-model", changed_by="test", source_ip="127.0.0.1")
    second_plan = service.build_plan(strategy="high_quality", provider_route=[], scoring_profile=profile)
    second = MockProvider([valid_result(memory_score=77)])
    result = service.analyze_photo(
        photo_id=ids[1], job_id=None, provider=second, strategy="high_quality", analysis_plan=second_plan
    )
    assert result["stage"] == "single_high"
    assert second.analyze_calls == 1


def test_cloud_strategy_prefilters_screenshot_without_token_usage(app, tmp_path):
    root = tmp_path / "screenshots"
    root.mkdir()
    Image.new("RGB", (900, 600), "white").save(root / "螢幕快照.png")
    photos = app.extensions["inktime_photo_repository"]
    PhotoScanner(
        photos,
        PhotoPreprocessor(),
        app.extensions["inktime_thumbnail_cache"],
    ).scan("截圖", root, build_thumbnails=False)
    with app.extensions["inktime_database"].session() as connection:
        photo_id = str(connection.execute("SELECT id FROM photos").fetchone()[0])
    provider = MockProvider([])
    photo = photos.get_with_path(photo_id)
    snapshot = app.extensions["inktime_analysis_service"].prefilter_snapshot(photo)

    result = app.extensions["inktime_analysis_service"].analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=provider,
        strategy="smart_two_stage",
    )

    assert result["stage"] == "prefilter"
    assert result["analysis"]["should_keep"] is False
    assert snapshot["decision"] == "auto_excluded"
    assert snapshot["primary_reason"] == "screenshot"
    assert any(check["key"] == "screenshot_strong" and check["hit"] for check in snapshot["checks"])
    assert provider.analyze_calls == 0
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0


def test_prefilter_persists_photo_analysis_and_audit_in_one_transaction(app, tmp_path):
    root = tmp_path / "screenshots"
    root.mkdir()
    Image.new("RGB", (900, 600), "white").save(root / "螢幕快照.png")
    photos = app.extensions["inktime_photo_repository"]
    PhotoScanner(photos, PhotoPreprocessor(), app.extensions["inktime_thumbnail_cache"]).scan(
        "截圖", root, build_thumbnails=False
    )
    with app.extensions["inktime_database"].session() as connection:
        photo_id = str(connection.execute("SELECT id FROM photos").fetchone()[0])
        connection.execute(
            "UPDATE photos SET eligible=1,exclusion_status='eligible',manual_override=0 WHERE id=?",
            (photo_id,),
        )
    result = app.extensions["inktime_analysis_service"].analyze_photo(
        photo_id=photo_id, job_id=None, provider=MockProvider([]), strategy="high_quality"
    )
    assert result["stage"] == "prefilter"
    with app.extensions["inktime_database"].session() as connection:
        photo = connection.execute(
            "SELECT eligible,exclusion_status,reject_reason FROM photos WHERE id=?", (photo_id,)
        ).fetchone()
        analysis = connection.execute(
            "SELECT stage,analysis_fingerprint FROM photo_analysis WHERE photo_id=? ORDER BY id DESC LIMIT 1",
            (photo_id,),
        ).fetchone()
        event = connection.execute(
            "SELECT event FROM photo_events WHERE photo_id=? ORDER BY id DESC LIMIT 1", (photo_id,)
        ).fetchone()
    assert tuple(photo) == (0, "auto_excluded", "screenshot")
    assert analysis["stage"] == "prefilter"
    assert analysis["analysis_fingerprint"]
    assert event["event"] == "automatic_exclusion"


def test_prefilter_does_not_overwrite_a_manual_restore(app, tmp_path):
    root = tmp_path / "screenshots"
    root.mkdir()
    Image.new("RGB", (900, 600), "white").save(root / "螢幕快照.png")
    photos = app.extensions["inktime_photo_repository"]
    PhotoScanner(photos, PhotoPreprocessor(), app.extensions["inktime_thumbnail_cache"]).scan(
        "截圖", root, build_thumbnails=False
    )
    with app.extensions["inktime_database"].session() as connection:
        photo_id = str(connection.execute("SELECT id FROM photos").fetchone()[0])
        connection.execute(
            "UPDATE photos SET eligible=1,exclusion_status='manually_restored',manual_override=1 WHERE id=?",
            (photo_id,),
        )
    photos.save_analysis(
        photo_id,
        None,
        "prefilter",
        "local",
        "local-prefilter",
        valid_result(),
        "{}",
        prefilter_evaluation={
            "decision": "auto_excluded",
            "primary_reason": "screenshot",
            "feature_version": "local-quality-v4",
        },
    )
    with app.extensions["inktime_database"].session() as connection:
        photo = connection.execute(
            "SELECT eligible,exclusion_status FROM photos WHERE id=?", (photo_id,)
        ).fetchone()
        event = connection.execute(
            "SELECT event FROM photo_events WHERE photo_id=? ORDER BY id DESC LIMIT 1", (photo_id,)
        ).fetchone()
    assert tuple(photo) == (1, "manually_restored")
    assert event["event"] == "automatic_exclusion_skipped"


def test_prefilter_transaction_rolls_back_photo_and_audit_when_analysis_insert_fails(app, tmp_path):
    root = tmp_path / "screenshots"
    root.mkdir()
    Image.new("RGB", (900, 600), "white").save(root / "螢幕快照.png")
    photos = app.extensions["inktime_photo_repository"]
    PhotoScanner(photos, PhotoPreprocessor(), app.extensions["inktime_thumbnail_cache"]).scan(
        "截圖", root, build_thumbnails=False
    )
    with app.extensions["inktime_database"].session() as connection:
        photo_id = str(connection.execute("SELECT id FROM photos").fetchone()[0])
        connection.execute(
            "UPDATE photos SET eligible=1,exclusion_status='eligible',manual_override=0 WHERE id=?",
            (photo_id,),
        )
        before = tuple(
            connection.execute(
                "SELECT eligible,exclusion_status,reject_reason FROM photos WHERE id=?", (photo_id,)
            ).fetchone()
        )
        connection.execute(
            "CREATE TRIGGER fail_prefilter_analysis BEFORE INSERT ON photo_analysis "
            f"WHEN NEW.photo_id='{photo_id}' BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
        )
    with pytest.raises(sqlite3.DatabaseError, match="forced failure"):
        app.extensions["inktime_analysis_service"].analyze_photo(
            photo_id=photo_id, job_id=None, provider=MockProvider([]), strategy="high_quality"
        )
    with app.extensions["inktime_database"].session() as connection:
        photo = connection.execute(
            "SELECT eligible,exclusion_status,reject_reason FROM photos WHERE id=?", (photo_id,)
        ).fetchone()
        events = connection.execute("SELECT COUNT(*) FROM photo_events WHERE photo_id=?", (photo_id,)).fetchone()[0]
    assert tuple(photo) == before
    assert events == 0


def test_prefilter_snapshot_requires_two_quality_defects(app, tmp_path):
    root = tmp_path / "quality"
    root.mkdir()
    Image.new("RGB", (900, 600), "gray").save(root / "plain.jpg")
    photos = app.extensions["inktime_photo_repository"]
    PhotoScanner(
        photos,
        PhotoPreprocessor(),
        app.extensions["inktime_thumbnail_cache"],
    ).scan("品質", root, build_thumbnails=False)
    with app.extensions["inktime_database"].session() as connection:
        photo_id = str(connection.execute("SELECT id FROM photos").fetchone()[0])

    snapshot = app.extensions["inktime_analysis_service"].prefilter_snapshot(
        photos.get_with_path(photo_id)
    )

    assert snapshot["decision"] == "auto_excluded"
    assert snapshot["primary_reason"] == "severe_blur"
    assert "severe_blur" in snapshot["matched_checks"]
