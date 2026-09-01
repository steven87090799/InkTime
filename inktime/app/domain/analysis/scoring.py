from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import math
from typing import Iterable, Mapping


DEFAULT_RANKING_WEIGHTS = {"memory": 50.0, "visual": 25.0, "local_quality": 25.0}
DEFAULT_FAVORITE_BONUS = 1
RANKING_RULE_VERSION = "ranking-v4"
SPECIAL_BONUSES = (0, 2, 5, 9, 14)


@dataclass(frozen=True)
class ScoreDistribution:
    """可在同一次請求內重複使用的排序分分布，避免逐張重排大型照片庫。"""

    values: tuple[float, ...]
    unique_count: int


def prepare_score_distribution(population: Iterable[float]) -> ScoreDistribution:
    finite_values: list[float] = []
    for value in population:
        numeric_value = float(value)
        if math.isfinite(numeric_value):
            finite_values.append(numeric_value)
    values = tuple(sorted(finite_values))
    return ScoreDistribution(values=values, unique_count=len(set(values)))


def calculate_library_percentile(
    score: float, population: Iterable[float] | ScoreDistribution
) -> float | None:
    """將原始排序分轉成照片庫內的相對位置；同分使用平均名次。"""
    distribution = (
        population if isinstance(population, ScoreDistribution) else prepare_score_distribution(population)
    )
    values = distribution.values
    if len(values) < 5 or distribution.unique_count < 3:
        return None
    value = float(score)
    left = bisect_left(values, value)
    right = bisect_right(values, value)
    average_index = float(left) if left == right else (left + right - 1) / 2.0
    percentile = average_index / (len(values) - 1) * 100.0
    return round(max(0.0, min(100.0, percentile)), 1)


def calculate_distinguishing_score(
    score: float, population: Iterable[float] | ScoreDistribution
) -> tuple[float, float | None]:
    """保留原始順序，同時拉開過度集中的模型分數。"""
    raw = max(0.0, min(100.0, float(score)))
    percentile = calculate_library_percentile(raw, population)
    if percentile is None:
        return round(raw, 1), None
    return round(raw * 0.35 + percentile * 0.65, 1), percentile


def score_band(percentile: float | None, score: float) -> str:
    marker = float(score) if percentile is None else percentile
    if marker >= 90:
        return "精選"
    if marker >= 75:
        return "推薦"
    if marker >= 40:
        return "一般"
    return "較弱"


def validate_ranking_weights(weights: Mapping[str, float]) -> dict[str, float]:
    if dict(weights) != DEFAULT_RANKING_WEIGHTS:
        raise ValueError("排序權重固定為回憶 50%、視覺 25%、本機品質 25%")
    return dict(DEFAULT_RANKING_WEIGHTS)


def ranking_components(analysis: Mapping, *, favorite: bool = False, rarity_adjustment: int = 0) -> dict:
    base = (
        float(analysis["memory_score"]) * 0.50
        + float(analysis["visual_score"]) * 0.25
        + float(analysis["local_quality_score"]) * 0.25
    )
    base = round(base, 2)
    effective = max(0, min(4, int(analysis["special_level"]) + int(bool(rarity_adjustment)) + int(favorite)))
    raw = round(max(0.0, min(100.0, base + SPECIAL_BONUSES[effective])), 2)
    return {
        "base_ranking_score": round(base, 2),
        "effective_special_level": effective,
        "library_rarity_adjustment": int(bool(rarity_adjustment)),
        "favorite_adjustment": int(favorite),
        "special_bonus": SPECIAL_BONUSES[effective],
        "raw_ranking_score": raw,
        "final_ranking_score": raw,
        "ranking_score": raw,
    }


def calculate_ranking_score(
    analysis: Mapping,
    weights: Mapping[str, float] | None = None,
    *,
    favorite: bool = False,
    favorite_bonus: float = DEFAULT_FAVORITE_BONUS,
    rarity_adjustment: int = 0,
) -> float:
    if weights is not None:
        validate_ranking_weights(weights)
    return ranking_components(analysis, favorite=favorite, rarity_adjustment=rarity_adjustment)[
        "raw_ranking_score"
    ]


def rarity_features(analysis: Mapping) -> set[str]:
    """Return value-bearing semantic rarity features.

    People count only refines an existing group-photo signal.  A statistically
    unusual crowd is not by itself evidence that a photo deserves a bonus.
    """
    people = int(analysis.get("people_count") or 0)
    codes = {str(value) for value in analysis.get("special_codes", [])}
    features = {f"special:{value}" for value in codes}
    types = {str(value) for value in analysis.get("types", [])}
    features.update(f"special:{code}|type:{value}" for code in codes for value in types)
    if "group_photo" in codes and people >= 16:
        features.add("special:group_photo|people:16+")
    return features


def library_rarity_adjustment(analysis: Mapping, population: Iterable[Mapping]) -> int:
    rows = list(population)
    if len(rows) < 20:
        return 0
    features = rarity_features(analysis)
    counts = {feature: 0 for feature in features}
    for row in rows:
        for feature in features & rarity_features(row):
            counts[feature] += 1
    # A small library is insufficient evidence; rare means at most 5% of peers.
    return int(any(count / len(rows) <= 0.05 for count in counts.values()))


DEFAULT_SCORING_RULES = """memory_score、visual_score 各為 0～100 數字，普通照片約 40～60，使用完整範圍。
memory 評一般情況下值得回看的程度：人物、互動、活動、日常紀錄與故事資訊量；不猜對使用者本人的重要性。重大事件、里程碑、大型合照與極罕見瞬間主要放 special_level，避免重複加分。
visual 只評構圖、光線、主體突出、色彩明暗、平衡及整體吸引力；女性、男性、孩子、寵物或旅行題材本身不加分。模糊、曝光、解析度與技術品質由本機計算。
special_level：0 普通；1 稍有特色；2 明確活動、合照、旅行紀錄或難得互動；3 重要典禮、舞台、大型合照或難重現事件；4 極罕見人生里程碑或無法重現的重要紀錄。非常保守使用 3、4，只填 level，不輸出 bonus。
special_codes 最多 2 個。group_photo 必須整群人為主體、共同面向鏡頭或明顯共同合影；夜市、觀眾、車站及街道人潮不算合照。people_count 只依可見人數。不宣稱照片在使用者照片庫少見，library rarity 由本機判定。"""
DISTINCTIVE_SCORING_RULES = DEFAULT_SCORING_RULES
