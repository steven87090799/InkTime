from __future__ import annotations

from inktime.app.services.benchmark_metrics import (
    calculate_benchmark_metrics,
    grade_agreement,
    orientation_metrics,
    should_keep_metrics,
    spearman_rank,
    top_k_overlap,
    type_metrics,
)


def test_grade_agreement_reports_exact_within_one_and_mae():
    result = grade_agreement(["S", "A", "E"], ["S", "B", "D"])
    assert round(result["exact"], 6) == round(1 / 3, 6)
    assert round(result["within_one"], 6) == 1
    assert round(result["mae"], 6) == round(2 / 3, 6)


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


def test_benchmark_metrics_keep_quality_and_ranking_separate():
    result = calculate_benchmark_metrics(
        [
            {
                "id": "a",
                "expected": {
                    "memory_grade": "A",
                    "beauty_grade": "A",
                    "technical_grade": "B",
                    "emotion_grade": "A",
                    "types": ["人物"],
                    "should_keep": True,
                    "visual_orientation": {"rotation_cw": 0, "ambiguous": False},
                },
                "predicted": {
                    "memory_grade": "A",
                    "beauty_grade": "B",
                    "technical_grade": "B",
                    "emotion_grade": "A",
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
                    "memory_grade": "B",
                    "beauty_grade": "B",
                    "technical_grade": "B",
                    "emotion_grade": "B",
                    "types": ["風景"],
                    "should_keep": False,
                    "visual_orientation": {"rotation_cw": 0, "ambiguous": False},
                },
                "predicted": {
                    "memory_grade": "B",
                    "beauty_grade": "B",
                    "technical_grade": "B",
                    "emotion_grade": "B",
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
    assert result["quality_metrics"]["grades"]["memory_grade"]["exact_grade_accuracy"] == 1
    assert result["ranking_metrics"]["spearman_rank_correlation"] == 1


def test_quality_metrics_accept_manual_golden_field_aliases():
    result = calculate_benchmark_metrics(
        [
            {
                "expected": {
                    "memory_grade": "A",
                    "beauty_grade": "A",
                    "technical_quality_grade": "B",
                    "emotion_grade": "A",
                    "types": ["風景"],
                    "should_keep": True,
                    "rotation_cw": 90,
                    "ambiguous": False,
                },
                "predicted": {
                    "memory_grade": "A",
                    "beauty_grade": "A",
                    "technical_grade": "B",
                    "emotion_grade": "A",
                    "types": ["風景"],
                    "should_keep": True,
                    "visual_orientation": {"rotation_cw": 90, "ambiguous": False},
                },
            }
        ]
    )
    assert result["quality_metrics"]["grades"]["technical_grade"]["exact_grade_accuracy"] == 1
    assert result["quality_metrics"]["orientation"]["rotation_exact_accuracy"] == 1
