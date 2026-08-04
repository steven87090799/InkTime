from __future__ import annotations

from pathlib import Path

import pytest

from inktime.app.services.model_benchmark import (
    BenchmarkError,
    ModelBenchmarkService,
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


def test_offline_benchmark_reports_no_network_or_production_mutation():
    report = ModelBenchmarkService().run_offline(axes=_axes(), sample_count=1, seed="test")
    assert report["mode"] == "offline-contract"
    assert report["network_invocations"] == 0
    assert report["production_mutations"] == 0
    assert report["quality_metrics"] is None
    assert report["ranking_metrics"] is None
