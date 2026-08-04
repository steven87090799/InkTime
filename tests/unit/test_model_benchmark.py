from __future__ import annotations

import json
from pathlib import Path

import pytest

from inktime.app.services.model_benchmark import (
    BenchmarkError,
    ModelBenchmarkService,
    _load_golden_records,
    _production_ranking_score,
    _select_benchmark_records,
)


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
