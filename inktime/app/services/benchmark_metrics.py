"""Dependency-free quality and ranking metrics for the benchmark contract."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import math
from typing import Any


GRADE_ORDER = {"E": 0, "D": 1, "C": 2, "B": 3, "A": 4, "S": 5}


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _grade(value: Any) -> int | None:
    key = str(value or "").strip().upper()
    return GRADE_ORDER.get(key)


def _field(record: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in record:
            return record[name]
    return default


def _orientation(record: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = record.get("visual_orientation")
    if isinstance(nested, Mapping):
        return nested
    rotation = record.get("rotation_cw")
    return {
        "rotation_cw": rotation,
        "ambiguous": bool(record.get("ambiguous")) if "ambiguous" in record else rotation is None,
    }


def grade_agreement(expected: Sequence[Any], predicted: Sequence[Any]) -> dict[str, Any]:
    """Return exact/within-one/MAE grade agreement without unknown inflation."""

    pairs = list(zip(expected, predicted, strict=True))
    # Keep the loop explicit so invalid/unknown grades are excluded from the
    # distance metrics rather than silently treated as the lowest grade.
    exact = 0
    within_one = 0
    known_expected = 0
    valid_distances: list[int] = []
    for expected_value, predicted_value in pairs:
        expected_grade = _grade(expected_value)
        predicted_grade = _grade(predicted_value)
        if expected_grade is not None:
            known_expected += 1
        if expected_grade is not None and predicted_grade is not None:
            distance = abs(expected_grade - predicted_grade)
            valid_distances.append(distance)
            within_one += int(distance <= 1)
            exact += int(distance == 0)
    exact_rate = _rate(exact, known_expected)
    within_one_rate = _rate(within_one, known_expected)
    mean_absolute_distance = (
        round(sum(valid_distances) / len(valid_distances), 6) if valid_distances else None
    )
    return {
        "count": len(pairs),
        "valid_count": len(valid_distances),
        "exact_grade_accuracy": exact_rate,
        "within_one_grade_accuracy": within_one_rate,
        "mean_absolute_grade_distance": mean_absolute_distance,
        # Keep concise aliases for existing report consumers while exposing
        # the unambiguous metric names required by the quality contract.
        "exact": exact_rate,
        "within_one": within_one_rate,
        "mae": mean_absolute_distance,
    }


def type_metrics(expected: Sequence[Iterable[Any]], predicted: Sequence[Iterable[Any]]) -> dict[str, Any]:
    """Calculate micro precision/recall/F1/Jaccard for multi-label types."""

    pairs = list(zip(expected, predicted, strict=True))
    true_positive = false_positive = false_negative = 0
    jaccards: list[float] = []
    for expected_types, predicted_types in pairs:
        expected_set = {str(value) for value in expected_types}
        predicted_set = {str(value) for value in predicted_types}
        true_positive += len(expected_set & predicted_set)
        false_positive += len(predicted_set - expected_set)
        false_negative += len(expected_set - predicted_set)
        union = expected_set | predicted_set
        jaccards.append(len(expected_set & predicted_set) / len(union) if union else 1.0)
    precision = _rate(true_positive, true_positive + false_positive)
    recall = _rate(true_positive, true_positive + false_negative)
    f1 = (
        round(2 * precision * recall / (precision + recall), 6)
        if precision is not None and recall is not None and precision + recall
        else 0.0
        if precision == 0 and recall == 0
        else None
    )
    return {
        "count": len(pairs),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "jaccard": round(sum(jaccards) / len(jaccards), 6) if jaccards else None,
        "micro_precision": precision,
        "micro_recall": recall,
        "micro_f1": f1,
        "jaccard_similarity": round(sum(jaccards) / len(jaccards), 6) if jaccards else None,
    }


def should_keep_metrics(expected: Sequence[Any], predicted: Sequence[Any]) -> dict[str, Any]:
    expected_values = [bool(value) for value in expected]
    predicted_values = [bool(value) for value in predicted]
    pairs = list(zip(expected_values, predicted_values, strict=True))
    true_positive = sum(left and right for left, right in pairs)
    false_positive = sum((not left) and right for left, right in pairs)
    false_negative = sum(left and (not right) for left, right in pairs)
    accuracy = sum(left == right for left, right in pairs)
    precision = _rate(true_positive, true_positive + false_positive)
    recall = _rate(true_positive, true_positive + false_negative)
    f1 = (
        round(2 * precision * recall / (precision + recall), 6)
        if precision is not None and recall is not None and precision + recall
        else 0.0
        if precision == 0 and recall == 0
        else None
    )
    return {
        "count": len(pairs),
        "accuracy": _rate(accuracy, len(pairs)),
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def orientation_metrics(expected: Sequence[Mapping[str, Any]], predicted: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pairs = list(zip(expected, predicted, strict=True))
    exact_unambiguous = 0
    unambiguous_expected = 0
    ambiguous_expected = 0
    ambiguous_predicted_correct = 0
    false_confident = 0
    for expected_value, predicted_value in pairs:
        expected_rotation = expected_value.get("rotation_cw")
        predicted_rotation = predicted_value.get("rotation_cw")
        expected_ambiguous = bool(expected_value.get("ambiguous")) or expected_rotation is None
        predicted_ambiguous = bool(predicted_value.get("ambiguous")) or predicted_rotation is None
        if not expected_ambiguous:
            unambiguous_expected += 1
            exact_unambiguous += int(
                expected_rotation == predicted_rotation and not predicted_ambiguous
            )
        if expected_ambiguous:
            ambiguous_expected += 1
            ambiguous_predicted_correct += int(predicted_ambiguous)
            false_confident += int(not predicted_ambiguous)
    return {
        "count": len(pairs),
        "rotation_exact_accuracy": _rate(exact_unambiguous, unambiguous_expected),
        "exact": _rate(exact_unambiguous, unambiguous_expected),
        "unambiguous_count": unambiguous_expected,
        "ambiguous_count": ambiguous_expected,
        "ambiguous_rate": _rate(ambiguous_predicted_correct, ambiguous_expected),
        "false_confident_count": false_confident,
        "false_confident_rate": _rate(false_confident, ambiguous_expected),
        "false_confident_orientation_rate": _rate(false_confident, ambiguous_expected),
    }


def _rank_map(values: Any) -> dict[str, float]:
    """Build one-based average ranks from ids, scores, or id/score pairs."""

    if isinstance(values, Mapping):
        pairs = [(str(key), float(score)) for key, score in values.items()]
    else:
        sequence = list(values or [])
        pairs = []
        for index, value in enumerate(sequence):
            if isinstance(value, (tuple, list)) and len(value) == 2 and isinstance(value[1], (int, float)):
                pairs.append((str(value[0]), float(value[1])))
            else:
                # A plain ordered id list is already a ranking.  Larger
                # synthetic scores keep the same order and make ties explicit
                # only when callers provide id/score pairs.
                pairs.append((str(value), float(len(sequence) - index)))
    pairs.sort(key=lambda item: (-item[1], item[0]))
    ranks: dict[str, float] = {}
    index = 0
    while index < len(pairs):
        end = index + 1
        while end < len(pairs) and pairs[end][1] == pairs[index][1]:
            end += 1
        average_rank = (index + 1 + end) / 2
        for key, _score in pairs[index:end]:
            ranks[key] = average_rank
        index = end
    return ranks


def spearman_rank(expected: Any, predicted: Any) -> float | None:
    expected_ranks = _rank_map(expected)
    predicted_ranks = _rank_map(predicted)
    keys = sorted(set(expected_ranks) & set(predicted_ranks))
    if len(keys) < 2:
        return None
    left = [expected_ranks[key] for key in keys]
    right = [predicted_ranks[key] for key in keys]
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    covariance = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    left_variance = sum((a - left_mean) ** 2 for a in left)
    right_variance = sum((b - right_mean) ** 2 for b in right)
    if left_variance == 0 or right_variance == 0:
        return 1.0 if left == right else 0.0
    return round(covariance / math.sqrt(left_variance * right_variance), 6)


def top_k_overlap(expected: Any, predicted: Any, ks: Sequence[int] = (10, 25, 50)) -> dict[str, Any]:
    expected_ranks = _rank_map(expected)
    predicted_ranks = _rank_map(predicted)
    keys = set(expected_ranks) & set(predicted_ranks)
    result: dict[str, Any] = {}
    for requested_k in ks:
        effective_k = min(max(1, int(requested_k)), len(keys)) if keys else 0
        expected_top = {key for key in keys if expected_ranks[key] <= effective_k}
        predicted_top = {key for key in keys if predicted_ranks[key] <= effective_k}
        result[str(requested_k)] = {
            "requested_k": int(requested_k),
            "effective_k": effective_k,
            "overlap_count": len(expected_top & predicted_top),
            "overlap_rate": _rate(len(expected_top & predicted_top), effective_k),
        }
    return result


def calculate_quality_metrics(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    expected = [item.get("expected") or item.get("golden") or {} for item in items]
    predicted = [item.get("predicted") or {} for item in items]
    grade_fields = {
        name: grade_agreement(
            [
                _field(
                    row,
                    name,
                    "technical_quality_grade" if name == "technical_grade" else name,
                )
                for row in expected
            ],
            [
                _field(
                    row,
                    name,
                    "technical_quality_grade" if name == "technical_grade" else name,
                )
                for row in predicted
            ],
        )
        for name in ("memory_grade", "beauty_grade", "technical_grade", "emotion_grade")
    }
    return {
        "count": len(items),
        "grades": grade_fields,
        "types": type_metrics(
            [row.get("types") or [] for row in expected],
            [row.get("types") or [] for row in predicted],
        ),
        "should_keep": should_keep_metrics(
            [row.get("should_keep") for row in expected],
            [row.get("should_keep") for row in predicted],
        ),
        "orientation": orientation_metrics(
            [_orientation(row) for row in expected],
            [_orientation(row) for row in predicted],
        ),
    }


def calculate_ranking_metrics(items: Sequence[Mapping[str, Any]], ks: Sequence[int] = (10, 25, 50)) -> dict[str, Any]:
    expected: list[tuple[str, float]] = []
    predicted: list[tuple[str, float]] = []
    for index, item in enumerate(items):
        identifier = str(item.get("id") or item.get("photo_id") or f"item-{index}")
        expected_rank = item.get("expected_rank")
        expected_score = item.get("expected_score")
        predicted_rank = item.get("predicted_rank")
        predicted_score = item.get("predicted_score")
        if expected_rank is None and expected_score is None:
            continue
        if predicted_rank is None and predicted_score is None:
            continue
        expected.append((identifier, -float(expected_rank) if expected_rank is not None else float(expected_score)))
        predicted.append((identifier, -float(predicted_rank) if predicted_rank is not None else float(predicted_score)))
    count = len(set(identifier for identifier, _value in expected) & set(identifier for identifier, _value in predicted))
    return {
        "count": count,
        "spearman": spearman_rank(expected, predicted),
        "spearman_rank_correlation": spearman_rank(expected, predicted),
        "top_k_overlap": top_k_overlap(expected, predicted, ks),
    }


def calculate_benchmark_metrics(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "quality_metrics": calculate_quality_metrics(items),
        "ranking_metrics": calculate_ranking_metrics(items),
    }
