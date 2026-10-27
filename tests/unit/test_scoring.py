from __future__ import annotations

import pytest
from PIL import Image

from inktime.app.domain.analysis.scoring import (
    calculate_ranking_score,
    FAVORITE_SPECIAL_LEVEL_BOOST,
    LOCAL_QUALITY_SCORE_KIND,
    SPECIAL_BONUSES,
    ranking_components,
    resolve_score_kind,
    score_band,
)
from inktime.app.providers.base import ProviderResponse, Usage, VisionProvider
from inktime.app.providers.openai_compatible import ProviderHTTPError
from tests.conftest import create_admin, csrf, login
from tests.unit.test_analysis_schema import valid_result


SCORES = {
    "memory_score": 80,
    "visual_score": 70,
    "local_quality_score": 60,
    "special_level": 0,
}
def test_favorite_only_boosts_special_level_without_a_fixed_score_bonus():
    ordinary = calculate_ranking_score(SCORES)
    favorite = calculate_ranking_score(SCORES, favorite=True)
    assert ordinary == 76.7
    assert favorite == 78.7
    assert FAVORITE_SPECIAL_LEVEL_BOOST == 1
    assert favorite - ordinary == SPECIAL_BONUSES[1] - SPECIAL_BONUSES[0]
    level_two = {**SCORES, "special_level": 2}
    ordinary_parts = ranking_components(level_two)
    favorite_parts = ranking_components(level_two, favorite=True)
    assert favorite_parts["base_ranking_score"] == ordinary_parts["base_ranking_score"]
    assert favorite_parts["favorite_special_level_boost"] == 1
    assert favorite_parts["effective_special_level"] == 3
    assert favorite_parts["ranking_score"] - ordinary_parts["ranking_score"] == 4


def test_score_kind_does_not_misclassify_a_real_local_ai_provider():
    assert resolve_score_kind(provider="local-ollama", stage="single") == "semantic"
    assert resolve_score_kind(provider="local", stage="single") == LOCAL_QUALITY_SCORE_KIND
    assert resolve_score_kind(provider="test-ai", stage="prefilter") == LOCAL_QUALITY_SCORE_KIND


@pytest.mark.parametrize("local_score", [None, 0, 40, 100])
def test_local_quality_does_not_change_model_ranking(local_score):
    assert calculate_ranking_score({**SCORES, "local_quality_score": local_score}) == 76.7


def test_score_labels_use_the_model_score_without_library_calibration():
    assert score_band(91) == "精選"
    assert score_band(78) == "推薦"
    assert score_band(55) == "一般"


@pytest.mark.parametrize("length", [77, 78, 80])
def test_long_scoring_names_restore_with_source_audit_and_preserve_original(client, app, length):
    import json
    user_id = create_admin(app)
    login(client)
    repository = app.extensions["inktime_scoring_repository"]
    original = repository.create(name="名" * length, rules=repository.current()["rules"], created_by=user_id, source_ip="test")
    repository.create(name="replacement", rules=original["rules"], created_by=user_id, source_ip="test")
    for _ in range(2):
        response = client.post(f"/api/v1/scoring/profiles/{original['id']}/restore", json={}, headers={"X-CSRF-Token": csrf(client)})
        assert response.status_code == 201
        restored = repository.get(response.get_json()["id"])
        assert len(restored["name"]) <= 80
        assert restored["rules"] == original["rules"]
        assert (restored["memory_weight"], restored["visual_weight"], restored["local_weight"]) == (67, 33, 0)
        assert repository.get(original["id"])["name"] == "名" * length
        with app.extensions["inktime_database"].session() as connection:
            audit = json.loads(connection.execute("SELECT details_json FROM activity_events WHERE source='scoring' AND source_id=?", (restored["id"],)).fetchone()[0])
        assert audit["restored_from_version_id"] == original["id"]


def test_scoring_profile_create_and_restore_are_versioned(app):
    user_id = create_admin(app)
    repository = app.extensions["inktime_scoring_repository"]
    initial = repository.current()
    rules = str(initial["rules"]) + "\n- 測試版本：真實互動再提高回憶分。"

    created = repository.create(
        name="家庭照片優先",
        rules=rules,
        created_by=user_id,
        source_ip="127.0.0.1",
    )

    assert created["is_active"] == 1
    assert created["memory_weight"] == 67
    assert created["ranking_contract_version"] == 4
    assert "favorite_bonus" not in created
    assert created["legacy_favorite_score_bonus"] == 0
    assert (created["visual_weight"], created["local_weight"]) == (33, 0)
    assert repository.get(str(initial["id"]))["is_active"] == 0
    with app.extensions["inktime_database"].session() as connection:
        history_count = connection.execute(
            "SELECT COUNT(*) FROM setting_history WHERE changed_by=?", (user_id,)
        ).fetchone()[0]
        snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM settings_snapshots WHERE actor_id=?", (user_id,)
        ).fetchone()[0]
    assert history_count == 1
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
        import json

        return ProviderResponse(
            json.dumps(valid_result(types=["未知分類"]), ensure_ascii=False),
            Usage(120, 30, 10),
        )

    def repair_json(self, **_kwargs):
        self.repair_calls += 1
        import json

        return ProviderResponse(json.dumps(valid_result(), ensure_ascii=False), Usage(40, 12, 2))


@pytest.mark.parametrize("invalid", ["legacy", "length", "caption"])
def test_scoring_lab_uses_current_plan_and_rejects_incompatible_answers(app, tmp_path, monkeypatch, invalid):
    import json
    from inktime.app.domain.analysis.schema import AnalysisValidationError

    settings = app.extensions["inktime_settings_repository"]
    settings.update("model.analysis_model", "openai/gpt-6-luna", changed_by="test", source_ip="test")
    settings.update("model.high_model", "legacy-model", changed_by="test", source_ip="test")
    settings.update("analysis.side_caption_min_chars", 12, changed_by="test", source_ip="test")
    settings.update("analysis.side_caption_max_chars", 14, changed_by="test", source_ip="test")
    service = app.extensions["inktime_scoring_lab_service"]
    requests = []

    class CaptureLabProvider(LabProvider):
        def analyze(self, **kwargs):
            requests.append(kwargs)
            value = valid_result()
            value["side_caption"] = "字" * (10 if invalid == "caption" else 12)
            if invalid == "legacy":
                value["schema_version"] = 4
            return ProviderResponse(json.dumps(value), Usage(120, 30, 10),
                                    finish_reason="length" if invalid == "length" else "stop")

    def build_router(route, **kwargs):
        assert kwargs["caption_controls"]["side_caption_min_chars"] == 12
        return CaptureLabProvider()

    monkeypatch.setattr(service.providers, "build_router", build_router)
    image = tmp_path / "current-plan.jpg"
    Image.new("RGB", (64, 64), "orange").save(image)
    with pytest.raises(AnalysisValidationError):
        service.analyze(image)
    assert len(requests) == 1
    assert requests[0]["model"] == "openai/gpt-6-luna"
    assert requests[0]["caption_controls"]["side_caption_max_chars"] == 14
    assert requests[0]["reasoning_effort"] == "none"
    assert requests[0]["max_tokens"] == 512
    with service.usage.database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 1


class FailingScoringProvider(LabProvider):
    def __init__(self, *, vision_error=None, repair_error=None):
        self.vision_error = vision_error
        self.repair_error = repair_error

    def analyze(self, **_kwargs):
        if self.vision_error is not None:
            raise self.vision_error
        import json

        return ProviderResponse(
            json.dumps(valid_result(types=["未知分類"]), ensure_ascii=False),
            Usage(120, 30, 10),
        )

    def repair_json(self, **_kwargs):
        if self.repair_error is not None:
            raise self.repair_error
        raise AssertionError("測試 Provider 未設定 repair response")


def test_scoring_lab_records_usage_and_uses_current_profile(app, tmp_path, monkeypatch):
    service = app.extensions["inktime_scoring_lab_service"]
    monkeypatch.setattr(service.providers, "build_router", lambda *args, **kwargs: LabProvider())
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


@pytest.mark.parametrize(
    ("global_cap", "analysis_cap", "expected"),
    [(8000, 256, 256), (8000, 384, 384), (300, 1200, 300), (8000, 1200, 512)],
)
def test_scoring_lab_honors_analysis_and_global_token_limits(
    app, tmp_path, monkeypatch, global_cap, analysis_cap, expected,
):
    service = app.extensions["inktime_scoring_lab_service"]
    service.settings.update("budget.max_tokens", global_cap, changed_by="test", source_ip="test")
    service.settings.update("budget.full_analysis_max_tokens", analysis_cap, changed_by="test", source_ip="test")
    requests = []

    class CaptureLabProvider(LabProvider):
        def analyze(self, **kwargs):
            requests.append(kwargs)
            return super().analyze(**kwargs)

    monkeypatch.setattr(service.providers, "build_router", lambda *args, **kwargs: CaptureLabProvider())
    image_path = tmp_path / "token-cap.jpg"
    Image.new("RGB", (64, 64), "orange").save(image_path)

    service.analyze(image_path)

    assert len(requests) == 1
    assert requests[0]["max_tokens"] == expected


def test_scoring_lab_rejects_invalid_response_without_extra_paid_repair(app, tmp_path, monkeypatch):
    from inktime.app.domain.analysis.schema import AnalysisValidationError

    service = app.extensions["inktime_scoring_lab_service"]
    provider = RepairLabProvider()
    monkeypatch.setattr(service.providers, "build_router", lambda *args, **kwargs: provider)
    image_path = tmp_path / "repair.jpg"
    Image.new("RGB", (64, 64), "purple").save(image_path)
    with pytest.raises(AnalysisValidationError):
        service.analyze(image_path)
    assert provider.repair_calls == 0
    with app.extensions["inktime_database"].session() as connection:
        usage = connection.execute("SELECT request_type,estimated_cost FROM api_usage").fetchall()
    assert [tuple(row) for row in usage] == [("scoring_test_vision", 0.0015)]


def test_scoring_lab_records_ambiguous_vision_failure_without_inventing_cost(
    app, tmp_path, monkeypatch
):
    service = app.extensions["inktime_scoring_lab_service"]
    provider = FailingScoringProvider(
        vision_error=ProviderHTTPError("timeout", "VLM-AMBIGUOUS", ambiguous=True)
    )
    monkeypatch.setattr(service.providers, "build_router", lambda *args, **kwargs: provider)
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



def test_scoring_lab_does_not_record_pre_network_failure(
    app, tmp_path, monkeypatch
):
    service = app.extensions["inktime_scoring_lab_service"]
    provider = FailingScoringProvider(vision_error=ValueError("invalid configuration"))
    monkeypatch.setattr(service.providers, "build_router", lambda *args, **kwargs: provider)
    image_path = tmp_path / "preflight-failed.jpg"
    Image.new("RGB", (64, 64), "blue").save(image_path)

    with pytest.raises(ValueError, match="invalid configuration"):
        service.analyze(image_path)
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0
