from __future__ import annotations

import json
from PIL import Image

from inktime.app.domain.photos import PhotoPreprocessor, ThumbnailCache
from inktime.app.providers.base import ProviderResponse, Usage, VisionProvider
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.repositories.usage import UsageRepository
from inktime.app.services.analysis import PhotoAnalysisService
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
    assert snapshot["decision"] == "excluded_screenshot"
    assert snapshot["checks"][0]["label"] == "截圖機率"
    assert snapshot["checks"][0]["hit"] is True
    assert provider.analyze_calls == 0
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0


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

    assert snapshot["decision"] == "excluded_low_quality"
    assert snapshot["required_defects"] == 2
    assert snapshot["defect_count"] >= 2
    assert "嚴重模糊或失焦" in snapshot["matched_defects"]
    assert "對比過低" in snapshot["matched_defects"]
