from __future__ import annotations

import pytest

from inktime.app.services.benchmark_metrics import (
    calculate_benchmark_metrics,
    orientation_metrics,
    score_agreement,
    should_keep_metrics,
    spearman_rank,
    top_k_overlap,
    type_metrics,
)


def test_score_agreement_reports_numeric_mae():
    result = score_agreement([100, 80, 0], [100, 70, 10])
    assert result["count"] == 3
    assert result["mae"] == pytest.approx(20 / 3)


def test_type_f1_and_should_keep_metrics_are_bounded():
    types = type_metrics([["人物", "旅行"], ["貓咪"]], [["人物"], ["狗"]])
    assert types["precision"] == 1 / 2
    assert round(types["recall"], 6) == round(1 / 3, 6)
    keep = should_keep_metrics([True, False, True], [True, True, False])
    assert round(keep["accuracy"], 6) == round(1 / 3, 6)
    assert keep["f1"] == 0.5


def test_orientation_metrics_distinguish_ambiguous_and_false_confident():
    result = orientation_metrics(
        [{"rotation_cw": None, "ambiguous": True}, {"rotation_cw": 90, "ambiguous": False}],
        [{"rotation_cw": 0, "ambiguous": False}, {"rotation_cw": 90, "ambiguous": False}],
    )
    assert result["false_confident_count"] == 1
    assert result["ambiguous_rate"] == 0


def test_spearman_and_top_k_use_bounded_tie_aware_ranks():
    assert spearman_rank([("a", 3), ("b", 2), ("c", 1)], [("a", 3), ("b", 2), ("c", 1)]) == 1
    overlap = top_k_overlap(["a", "b"], ["b", "a"], ks=(10, 25, 50))
    assert overlap["10"]["effective_k"] == 2
    assert overlap["10"]["overlap_rate"] == 1


def test_top_k_boundary_ties_are_deterministic_and_exact_k():
    overlap = top_k_overlap(
        [("c", 100), ("b", 100), ("a", 100)],
        [("a", 100), ("c", 100), ("b", 100)],
        ks=(2,),
    )
    assert overlap["2"]["effective_k"] == 2
    assert overlap["2"]["overlap_count"] == 2
    assert overlap["2"]["overlap_rate"] == 1


def test_top_k_handles_small_and_empty_datasets_without_exceeding_one():
    small = top_k_overlap([("b", 2), ("a", 1)], [("a", 2), ("b", 1)], ks=(10,))
    assert small["10"]["effective_k"] == 2
    assert 0 <= small["10"]["overlap_rate"] <= 1

    empty = top_k_overlap([], [], ks=(10,))
    assert empty["10"]["effective_k"] == 0
    assert empty["10"]["overlap_rate"] is None


def test_benchmark_metrics_keep_quality_and_ranking_separate():
    result = calculate_benchmark_metrics(
        [
            {
                "id": "a",
                "expected": {
                    "memory_score": 90,
                    "visual_score": 80,
                    "special_level": 2,
                    "types": ["人物"],
                    "should_keep": True,
                    "visual_orientation": {"rotation_cw": 0, "ambiguous": False},
                },
                "predicted": {
                    "memory_score": 90,
                    "visual_score": 75,
                    "special_level": 2,
                    "types": ["人物"],
                    "should_keep": True,
                    "visual_orientation": {"rotation_cw": 0, "ambiguous": False},
                },
                "expected_score": 10,
                "predicted_score": 9,
            },
            {
                "id": "b",
                "expected": {
                    "memory_score": 60,
                    "visual_score": 60,
                    "special_level": 0,
                    "types": ["風景"],
                    "should_keep": False,
                    "visual_orientation": {"rotation_cw": 0, "ambiguous": False},
                },
                "predicted": {
                    "memory_score": 60,
                    "visual_score": 60,
                    "special_level": 0,
                    "types": ["風景"],
                    "should_keep": False,
                    "visual_orientation": {"rotation_cw": 0, "ambiguous": False},
                },
                "expected_score": 5,
                "predicted_score": 4,
            }
        ]
    )
    assert "quality_metrics" in result
    assert "ranking_metrics" in result
    assert result["quality_metrics"]["scores"]["memory_score"]["mae"] == 0
    assert result["ranking_metrics"]["spearman_rank_correlation"] == 1


def test_quality_metrics_use_v4_scores_and_orientation():
    result = calculate_benchmark_metrics(
        [
            {
                "expected": {
                    "memory_score": 80,
                    "visual_score": 70,
                    "special_level": 1,
                    "types": ["風景"],
                    "should_keep": True,
                    "rotation_cw": 90,
                    "ambiguous": False,
                },
                "predicted": {
                    "memory_score": 80,
                    "visual_score": 70,
                    "special_level": 1,
                    "types": ["風景"],
                    "should_keep": True,
                    "visual_orientation": {"rotation_cw": 90, "ambiguous": False},
                },
            }
        ]
    )
    assert result["quality_metrics"]["scores"]["visual_score"]["mae"] == 0
    assert result["quality_metrics"]["orientation"]["rotation_exact_accuracy"] == 1


def test_ranking_duplicate_identifiers_fail_closed():
    with pytest.raises(ValueError, match="unique"):
        calculate_benchmark_metrics(
            [
                {
                    "id": "duplicate",
                    "expected_rank": 1,
                    "predicted_rank": 1,
                },
                {
                    "id": "duplicate",
                    "expected_rank": 2,
                    "predicted_rank": 2,
                },
            ]
        )
