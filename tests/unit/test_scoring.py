from __future__ import annotations

import pytest
from PIL import Image

from inktime.app.domain.analysis.scoring import (
    calculate_distinguishing_score,
    calculate_library_percentile,
    calculate_ranking_score,
    LOCAL_QUALITY_SCORE_KIND,
    prepare_score_distribution,
    resolve_score_kind,
    score_band,
    validate_ranking_weights,
)
from inktime.app.providers.base import ProviderResponse, Usage, VisionProvider
from inktime.app.providers.openai_compatible import ProviderHTTPError
from tests.conftest import create_admin
from tests.unit.test_analysis_schema import valid_result


SCORES = {
    "memory_score": 80,
    "beauty_score": 70,
    "technical_quality_score": 60,
    "emotion_score": 90,
}
WEIGHTS = {
    "memory": 50,
    "beauty": 20,
    "technical_quality": 10,
    "emotion": 20,
}


def test_ranking_score_preserves_components_and_applies_favorite_bonus():
    assert calculate_ranking_score(SCORES, WEIGHTS) == 78
    assert calculate_ranking_score(SCORES, WEIGHTS, favorite=True, favorite_bonus=5) == 83


def test_ranking_weights_must_total_one_hundred():
    with pytest.raises(ValueError, match="100%"):
        validate_ranking_weights({**WEIGHTS, "memory": 49})


def test_score_kind_does_not_misclassify_a_real_local_ai_provider():
    assert resolve_score_kind(provider="local-ollama", stage="single") == "semantic"
    assert resolve_score_kind(provider="local", stage="single") == LOCAL_QUALITY_SCORE_KIND
    assert resolve_score_kind(provider="test-ai", stage="prefilter") == LOCAL_QUALITY_SCORE_KIND


def test_library_percentile_spreads_narrow_scores_without_changing_order():
    population = [70, 71, 72, 73, 74]

    low, low_percentile = calculate_distinguishing_score(70, population)
    middle, middle_percentile = calculate_distinguishing_score(72, population)
    high, high_percentile = calculate_distinguishing_score(74, population)

    assert (low_percentile, middle_percentile, high_percentile) == (0.0, 50.0, 100.0)
    assert low < middle < high
    assert high - low > 40
    assert score_band(high_percentile, high) == "精選"


def test_library_percentile_uses_average_rank_for_ties_and_falls_back_for_small_samples():
    assert calculate_library_percentile(70, [70, 70, 80, 90, 100]) == 12.5
    assert calculate_library_percentile(0, [70, 70, 80, 90, 100]) == 0.0
    assert calculate_library_percentile(110, [70, 70, 80, 90, 100]) == 100.0
    assert calculate_distinguishing_score(82, [80, 81, 82, 83]) == (82.0, None)


def test_prepared_distribution_can_be_reused_for_large_photo_lists():
    distribution = prepare_score_distribution([74, 70, 73, 71, 72])

    assert distribution.values == (70.0, 71.0, 72.0, 73.0, 74.0)
    assert calculate_library_percentile(72, distribution) == 50.0


def test_scoring_profile_create_and_restore_are_versioned(app):
    user_id = create_admin(app)
    repository = app.extensions["inktime_scoring_repository"]
    initial = repository.current()
    rules = str(initial["rules"]) + "\n- 測試版本：真實互動再提高回憶分。"

    created = repository.create(
        name="家庭照片優先",
        rules=rules,
        weights={
            "memory": 55,
            "beauty": 15,
            "technical_quality": 10,
            "emotion": 20,
        },
        favorite_bonus=8,
        created_by=user_id,
        source_ip="127.0.0.1",
    )

    assert created["is_active"] == 1
    assert created["memory_weight"] == 55
    assert repository.get(str(initial["id"]))["is_active"] == 0
    assert app.extensions["inktime_settings_repository"].get("analysis.ranking_memory_weight") == 55
    with app.extensions["inktime_database"].session() as connection:
        history_count = connection.execute(
            "SELECT COUNT(*) FROM setting_history WHERE changed_by=?", (user_id,)
        ).fetchone()[0]
        snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM settings_snapshots WHERE actor_id=?", (user_id,)
        ).fetchone()[0]
    assert history_count == 4
    assert snapshot_count == 1

    restored = repository.restore(str(initial["id"]), created_by=user_id, source_ip="127.0.0.1")
    assert restored["is_active"] == 1
    assert restored["name"].startswith("還原：")
    assert restored["memory_weight"] == initial["memory_weight"]
    assert len(repository.list()) == 3
    with app.extensions["inktime_database"].session() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM settings_snapshots WHERE actor_id=?", (user_id,)
            ).fetchone()[0]
            == 2
        )


class LabProvider(VisionProvider):
    name = "測試 Provider"

    def analyze(self, **_kwargs):
        import json

        return ProviderResponse(json.dumps(valid_result(), ensure_ascii=False), Usage(120, 30, 10))

    def repair_json(self, **_kwargs):
        raise AssertionError("有效回應不應進入修復")

    def submit_batch(self, requests, completion_window="24h"):
        raise NotImplementedError

    def poll_batch(self, batch_id):
        raise NotImplementedError

    def cancel_batch(self, batch_id):
        raise NotImplementedError

    def estimate_cost(self, model, usage):
        return 0.0015

    def validate_config(self):
        return True, "ok"


class RepairLabProvider(LabProvider):
    def __init__(self):
        self.repair_calls = 0

    def analyze(self, **_kwargs):
        return ProviderResponse('{"memory_score": 1}', Usage(120, 30, 10))

    def repair_json(self, **_kwargs):
        self.repair_calls += 1
        import json

        return ProviderResponse(json.dumps(valid_result(), ensure_ascii=False), Usage(40, 12, 2))


class FailingScoringProvider(LabProvider):
    def __init__(self, *, vision_error=None, repair_error=None):
        self.vision_error = vision_error
        self.repair_error = repair_error

    def analyze(self, **_kwargs):
        if self.vision_error is not None:
            raise self.vision_error
        return ProviderResponse('{"memory_score": 1}', Usage(120, 30, 10))

    def repair_json(self, **_kwargs):
        if self.repair_error is not None:
            raise self.repair_error
        raise AssertionError("測試 Provider 未設定 repair response")


def test_scoring_lab_records_usage_and_uses_current_profile(app, tmp_path, monkeypatch):
    service = app.extensions["inktime_scoring_lab_service"]
    monkeypatch.setattr(service.providers, "build_router", lambda: LabProvider())
    image_path = tmp_path / "test.jpg"
    Image.new("RGB", (64, 64), "orange").save(image_path)

    result = service.analyze(image_path)

    assert result["profile"]["name"] == "預設評分規則"
    assert result["ranking_score"] >= 0
    assert result["usage"]["cost"] == 0.0015
    with app.extensions["inktime_database"].session() as connection:
        usage = connection.execute(
            "SELECT request_type,input_tokens,output_tokens,cached_tokens FROM api_usage"
        ).fetchall()
    assert [tuple(row) for row in usage] == [("scoring_test_vision", 120, 30, 10)]


def test_scoring_lab_records_vision_and_text_repair_as_separate_attempts(app, tmp_path, monkeypatch):
    service = app.extensions["inktime_scoring_lab_service"]
    provider = RepairLabProvider()
    monkeypatch.setattr(service.providers, "build_router", lambda: provider)
    image_path = tmp_path / "repair.jpg"
    Image.new("RGB", (64, 64), "purple").save(image_path)

    result = service.analyze(image_path)

    assert provider.repair_calls == 1
    assert result["usage"]["attempts"]
    assert result["usage"]["cost"] == 0.003
    assert result["usage"]["vision_cost"] == 0.0015
    assert result["usage"]["repair_cost"] == 0.0015
    with app.extensions["inktime_database"].session() as connection:
        usage = connection.execute(
            "SELECT request_type,input_tokens,output_tokens,cached_tokens,image_bytes,estimated_cost "
            "FROM api_usage ORDER BY id"
        ).fetchall()
    assert [row["request_type"] for row in usage] == [
        "scoring_test_vision",
        "scoring_test_repair",
    ]
    assert usage[0]["image_bytes"] == image_path.stat().st_size
    assert usage[1]["image_bytes"] == 0
    assert [usage[0]["estimated_cost"], usage[1]["estimated_cost"]] == [0.0015, 0.0015]


def test_scoring_lab_records_ambiguous_vision_failure_without_inventing_cost(
    app, tmp_path, monkeypatch
):
    service = app.extensions["inktime_scoring_lab_service"]
    provider = FailingScoringProvider(
        vision_error=ProviderHTTPError("timeout", "VLM-AMBIGUOUS", ambiguous=True)
    )
    monkeypatch.setattr(service.providers, "build_router", lambda: provider)
    image_path = tmp_path / "ambiguous.jpg"
    Image.new("RGB", (64, 64), "orange").save(image_path)

    with pytest.raises(ProviderHTTPError):
        service.analyze(image_path)
    with app.extensions["inktime_database"].session() as connection:
        usage = connection.execute(
            "SELECT request_type,status,cost_source,actual_cost,image_bytes,error_code FROM api_usage"
        ).fetchone()
    assert tuple(usage) == (
        "scoring_test_vision",
        "failed",
        "unknown",
        None,
        image_path.stat().st_size,
        "VLM-AMBIGUOUS",
    )


def test_scoring_lab_keeps_completed_vision_and_failed_repair_attempt(
    app, tmp_path, monkeypatch
):
    service = app.extensions["inktime_scoring_lab_service"]
    provider = FailingScoringProvider(
        repair_error=ProviderHTTPError("reset", "VLM-AMBIGUOUS", ambiguous=True)
    )
    monkeypatch.setattr(service.providers, "build_router", lambda: provider)
    image_path = tmp_path / "repair-failed.jpg"
    Image.new("RGB", (64, 64), "purple").save(image_path)

    with pytest.raises(ProviderHTTPError):
        service.analyze(image_path)
    with app.extensions["inktime_database"].session() as connection:
        usage = connection.execute(
            "SELECT request_type,status,cost_source,actual_cost,image_bytes FROM api_usage ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in usage] == [
        ("scoring_test_vision", "completed", "estimated", None, image_path.stat().st_size),
        ("scoring_test_repair", "failed", "unknown", None, 0),
    ]


def test_scoring_lab_does_not_record_pre_network_failure(
    app, tmp_path, monkeypatch
):
    service = app.extensions["inktime_scoring_lab_service"]
    provider = FailingScoringProvider(vision_error=ValueError("invalid configuration"))
    monkeypatch.setattr(service.providers, "build_router", lambda: provider)
    image_path = tmp_path / "preflight-failed.jpg"
    Image.new("RGB", (64, 64), "blue").save(image_path)

    with pytest.raises(ValueError, match="invalid configuration"):
        service.analyze(image_path)
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0
