from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from inktime.app.providers.base import ProviderResponse, Usage
from inktime.app.services.model_benchmark import (
    BenchmarkError,
    ModelBenchmarkService,
    _load_golden_records,
    _production_ranking_score,
    _select_benchmark_records,
)
from tests.unit.test_analysis_schema import valid_result


def _axes():
    return ModelBenchmarkService.build_axes(
        provider="offline-synthetic",
        models=["offline-model"],
        image_sides=[512],
        prompt_profiles=["default"],
        variants=[False],
        reasoning_efforts=["none"],
    )


def test_live_benchmark_requires_explicit_confirmation():
    with pytest.raises(BenchmarkError, match="confirm-live-quality"):
        ModelBenchmarkService().run_live(
            axes=_axes(),
            sample_count=1,
            seed="test",
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            max_requests=2,
            max_cost=1,
            dataset=Path("approved.json"),
        )


def test_benchmark_excludes_never_upload_and_ineligible_records():
    selected, excluded = _select_benchmark_records(
        [
            {"photo_id": "private", "never_upload": True},
            {"photo_id": "inactive", "active": False},
            {"photo_id": "eligible", "active": True, "eligible": True},
        ],
        sample_count=10,
        seed="test",
    )
    assert [row["photo_id"] for row in selected] == ["eligible"]
    assert excluded["never_upload"] == 1
    assert excluded["inactive"] == 1


def test_golden_manifest_uses_canonical_exclusion_flags_before_network(tmp_path):
    expected = {
        "memory_grade": "A",
        "beauty_grade": "A",
        "technical_grade": "A",
        "emotion_grade": "A",
        "types": ["風景"],
        "should_keep": True,
        "rotation_cw": 0,
        "ambiguous": False,
    }
    (tmp_path / "inactive.jpg").write_bytes(b"fixture")
    (tmp_path / "eligible.jpg").write_bytes(b"fixture")
    (tmp_path / "private.jpg").write_bytes(b"fixture")
    manifest = {
        "version": "inktime-golden-v1",
        "dataset": {"id": "test", "source": "synthetic", "privacy": "non_private"},
        "items": [
            {"id": "inactive", "image": "inactive.jpg", "expected": expected, "inactive": True},
            {"id": "eligible", "image": "eligible.jpg", "expected": expected, "ineligible": True},
            {"id": "private", "image": "private.jpg", "expected": expected, "never_upload": True},
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    records = _load_golden_records(path)
    selected, excluded = _select_benchmark_records(records, sample_count=10, seed="test")
    assert selected == []
    assert excluded["inactive"] == 1
    assert excluded["ineligible"] == 1
    assert excluded["never_upload"] == 1

    class FailingProviderFactory:
        def __call__(self, **_kwargs):
            raise AssertionError("fully excluded manifest must not construct a Provider")

    report = ModelBenchmarkService(provider_factory=FailingProviderFactory()).run_live(
        axes=_axes(),
        sample_count=10,
        seed="test",
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        max_requests=2,
        max_cost=1,
        dataset=path,
        confirm_live_quality=True,
    )
    assert report["network_invocations"] == 0


def test_golden_manifest_unknown_field_fails_closed_before_network(tmp_path):
    manifest = {
        "version": "inktime-golden-v1",
        "dataset": {"id": "test", "source": "synthetic", "privacy": "non_private"},
        "items": [
            {
                "id": "item",
                "image": "item.jpg",
                "active": True,
                "expected": {
                    "memory_grade": "A",
                    "beauty_grade": "A",
                    "technical_grade": "A",
                    "emotion_grade": "A",
                    "types": ["風景"],
                    "should_keep": True,
                    "rotation_cw": 0,
                    "ambiguous": False,
                },
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "item.jpg").write_bytes(b"fixture")

    with pytest.raises(BenchmarkError, match="未知欄位"):
        _load_golden_records(path)


def test_benchmark_ranking_uses_production_weighted_ordering():
    lower_weighted = {
        "memory_score": 0,
        "beauty_score": 100,
        "technical_quality_score": 100,
        "emotion_score": 100,
    }
    memory_weighted = {
        "memory_score": 100,
        "beauty_score": 0,
        "technical_quality_score": 0,
        "emotion_score": 10,
    }
    assert sum(lower_weighted.values()) > sum(memory_weighted.values())
    assert _production_ranking_score(lower_weighted) < _production_ranking_score(memory_weighted)


def test_offline_benchmark_reports_no_network_or_production_mutation():
    report = ModelBenchmarkService().run_offline(axes=_axes(), sample_count=1, seed="test")
    assert report["mode"] == "offline-contract"
    assert report["network_invocations"] == 0
    assert report["production_mutations"] == 0
    assert report["quality_metrics"] is None
    assert report["ranking_metrics"] is None
    assert report["ranking_policy"]["ranking_rule_version"] == "ranking-v2"
    assert report["ranking_policy"]["ranking_weights"] == {
        "memory": 50.0,
        "beauty": 20.0,
        "technical_quality": 10.0,
        "emotion": 20.0,
    }
    assert report["ranking_policy"]["favorite_bonus_policy"]["applied"] is False


class _LiveMetricProvider:
    def __init__(self, *, invalid_first: bool):
        self.invalid_first = invalid_first
        self.analyze_count = 0
        self.last_request_metrics = {}

    def validate_config(self):
        return True, "ok"

    def analyze(self, **_kwargs):
        self.analyze_count += 1
        if self.invalid_first and self.analyze_count == 1:
            content = "not-json"
        else:
            content = json.dumps(valid_result(), ensure_ascii=False)
        return ProviderResponse(content, Usage(input_tokens=10, output_tokens=5, provider_reported_cost=0.1))

    def repair_json(self, **_kwargs):
        return ProviderResponse(json.dumps(valid_result(), ensure_ascii=False), Usage(input_tokens=4, output_tokens=2))

    def estimate_cost(self, _model, _usage):
        return None

    def close(self):
        return None


def _live_manifest(tmp_path: Path) -> Path:
    expected = {
        "memory_grade": "A",
        "beauty_grade": "A",
        "technical_grade": "A",
        "emotion_grade": "A",
        "types": ["風景"],
        "should_keep": True,
        "rotation_cw": 0,
        "ambiguous": False,
    }
    items = []
    for index in range(2):
        image_name = f"live-{index}.jpg"
        Image.new("RGB", (2, 2), "white").save(tmp_path / image_name, format="JPEG")
        items.append(
            {
                "id": f"live-{index}",
                "image": image_name,
                "expected": expected,
                "expected_score": 1.0,
            }
        )
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": "inktime-golden-v1",
                "dataset": {"id": "test", "source": "synthetic", "privacy": "non_private"},
                "items": items,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_live_metrics_separate_attempt_coverage_and_unknown_cost_denominator(tmp_path):
    manifest = _live_manifest(tmp_path)
    providers = []

    def factory(**_kwargs):
        provider = _LiveMetricProvider(invalid_first=True)
        providers.append(provider)
        return provider

    report = ModelBenchmarkService(provider_factory=factory).run_live(
        axes=_axes(),
        sample_count=2,
        seed="coverage",
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        max_requests=8,
        max_cost=1,
        dataset=manifest,
        confirm_live_quality=True,
    )
    metrics = report["axes"][0]["metrics"]
    assert metrics["selected_photos"] == 2
    assert metrics["attempted_photos"] == 1
    assert metrics["schema_valid_photos"] == 1
    assert metrics["quality_eligible_photos"] == 1
    assert metrics["ranking_eligible_photos"] == 1
    assert metrics["attempt_coverage_rate"] == 0.5
    assert metrics["schema_valid_coverage_rate"] == 1.0
    assert metrics["quality_coverage_rate"] == 1.0
    assert metrics["ranking_coverage_rate"] == 1.0
    assert metrics["unknown_cost_count"] == 1
    assert metrics["known_cost_total"] == 0.1
    assert metrics["cost_complete"] is False
    assert metrics["cost_denominator"] == "attempted_photos"
    assert metrics["avg_cost_per_attempted_photo"] is None
    assert metrics["avg_cost_per_photo"] is None
    assert metrics["quality_metrics"]["count"] == 1
    assert metrics["ranking_metrics"]["count"] == 1
    assert report["stopped_by_budget"] is True
    assert report["network_invocations"] == 3  # capability + vision + text-only repair
    assert len(providers) == 1


def test_live_budget_stop_still_emits_zero_denominator_axis_metrics(tmp_path):
    manifest = _live_manifest(tmp_path)

    def factory(**_kwargs):
        return _LiveMetricProvider(invalid_first=False)

    report = ModelBenchmarkService(provider_factory=factory).run_live(
        axes=_axes(),
        sample_count=2,
        seed="zero-denominator",
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        max_requests=1,
        max_cost=1,
        dataset=manifest,
        confirm_live_quality=True,
    )
    metrics = report["axes"][0]["metrics"]
    assert metrics["selected_photos"] == 2
    assert metrics["attempted_photos"] == 0
    assert metrics["attempt_coverage_rate"] is None
    assert metrics["schema_valid_coverage_rate"] is None
    assert metrics["avg_cost_per_attempted_photo"] is None
    assert metrics["cost_per_1000_attempted_photos"] is None
    assert metrics["quality_metrics"]["count"] == 0
    assert metrics["ranking_metrics"]["count"] == 0


def test_golden_manifest_rejects_duplicate_ids_and_mixed_aliases_before_provider(tmp_path):
    expected = {
        "memory_grade": "A",
        "beauty_grade": "A",
        "technical_grade": "A",
        "technical_quality_grade": "A",
        "emotion_grade": "A",
        "types": ["風景"],
        "should_keep": True,
        "visual_orientation": {"rotation_cw": 0, "ambiguous": False},
    }
    (tmp_path / "one.jpg").write_bytes(b"fixture")
    (tmp_path / "two.jpg").write_bytes(b"fixture")
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": "inktime-golden-v1",
                "dataset": {"id": "test", "source": "synthetic", "privacy": "non_private"},
                "items": [
                    {"id": "duplicate", "image": "one.jpg", "expected": expected},
                    {"id": "duplicate", "image": "two.jpg", "expected": expected},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkError, match="只包含一個 technical grade alias"):
        _load_golden_records(path)

    expected.pop("technical_quality_grade")
    path.write_text(
        json.dumps(
            {
                "version": "inktime-golden-v1",
                "dataset": {"id": "test", "source": "synthetic", "privacy": "non_private"},
                "items": [
                    {"id": "duplicate", "image": "one.jpg", "expected": expected},
                    {"id": "duplicate", "image": "two.jpg", "expected": expected},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkError, match="duplicate item id"):
        _load_golden_records(path)

    path.write_text(
        json.dumps(
            {
                "version": "inktime-golden-v1",
                "dataset": {"id": "test", "source": "synthetic", "privacy": "non_private"},
                "items": [
                    {"id": "one", "image": "one.jpg", "expected": expected},
                    {"id": "two", "image": "./one.jpg", "expected": expected},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkError, match="duplicate resolved image"):
        _load_golden_records(path)


@pytest.mark.parametrize(
    "case",
    ["mixed_orientation", "flat_missing_rotation", "flat_missing_ambiguous", "nested_missing_field"],
)
def test_golden_manifest_rejects_incomplete_or_mixed_orientation_aliases(tmp_path, case):
    path = _live_manifest(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = payload["items"][0]["expected"]
    if case == "mixed_orientation":
        expected["visual_orientation"] = {"rotation_cw": 0, "ambiguous": False}
    elif case == "flat_missing_rotation":
        expected.pop("rotation_cw")
    elif case == "flat_missing_ambiguous":
        expected.pop("ambiguous")
    else:
        expected.pop("rotation_cw")
        expected.pop("ambiguous")
        expected["visual_orientation"] = {"rotation_cw": 0}
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BenchmarkError, match="orientation"):
        _load_golden_records(path)


def test_golden_manifest_legacy_aliases_are_loaded_as_canonical_fields(tmp_path):
    path = _live_manifest(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = payload["items"][0]["expected"]
    expected["technical_quality_grade"] = expected.pop("technical_grade")
    expected["visual_orientation"] = {
        "rotation_cw": expected.pop("rotation_cw"),
        "ambiguous": expected.pop("ambiguous"),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    records = _load_golden_records(path)
    canonical = records[0]["expected"]
    assert canonical["technical_grade"] == "A"
    assert "technical_quality_grade" not in canonical
    assert canonical["visual_orientation"] == {"rotation_cw": 0, "ambiguous": False}
