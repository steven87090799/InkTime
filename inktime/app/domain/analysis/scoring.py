from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import math
from typing import Iterable, Mapping


DEFAULT_RANKING_WEIGHTS = {
    "memory": 50.0,
    "beauty": 20.0,
    "technical_quality": 10.0,
    "emotion": 20.0,
}
DEFAULT_FAVORITE_BONUS = 5.0
RANKING_RULE_VERSION = "ranking-v2"
LOCATION_RULE_VERSION = "travel-v1"
GRADE_TO_SCORE = {"S": 95.0, "A": 85.0, "B": 70.0, "C": 55.0, "D": 35.0, "E": 15.0}


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
        population
        if isinstance(population, ScoreDistribution)
        else prepare_score_distribution(population)
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
    required = set(DEFAULT_RANKING_WEIGHTS)
    if set(weights) != required:
        raise ValueError("綜合評分權重欄位不完整")
    normalized = {key: float(value) for key, value in weights.items()}
    if any(value < 0 or value > 100 for value in normalized.values()):
        raise ValueError("每項權重必須介於 0 到 100")
    if abs(sum(normalized.values()) - 100.0) > 0.001:
        raise ValueError("四項權重合計必須等於 100%")
    return normalized


def calculate_ranking_score(
    analysis: Mapping[str, float | int],
    weights: Mapping[str, float],
    *,
    favorite: bool = False,
    favorite_bonus: float = DEFAULT_FAVORITE_BONUS,
) -> float:
    values = validate_ranking_weights(weights)
    score = (
        float(analysis["memory_score"]) * values["memory"]
        + float(analysis["beauty_score"]) * values["beauty"]
        + float(analysis["technical_quality_score"]) * values["technical_quality"]
        + float(analysis["emotion_score"]) * values["emotion"]
    ) / 100.0
    if favorite:
        score += max(0.0, min(100.0, float(favorite_bonus)))
    return round(max(0.0, min(100.0, score)), 2)


def grade_to_score(value: str | None, fallback: float) -> float:
    """模型只能交付等級；數字映射固定在程式，便於版本化與重算。"""
    return GRADE_TO_SCORE.get(str(value or "").upper(), float(fallback))


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi, delta_lon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    value = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lon / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1.0 - value)))


def calculate_travel_bonus(
    *,
    latitude: float | None,
    longitude: float | None,
    home_latitude: float | None,
    home_longitude: float | None,
    home_radius_km: float,
    near_bonus: float,
    far_bonus: float,
    foreign_bonus: float,
    rare_bonus: float,
    foreign_country: bool = False,
    rare_location: bool = False,
    maximum: float = 8.0,
) -> tuple[float, float | None]:
    if latitude is None or longitude is None or home_latitude is None or home_longitude is None:
        return 0.0, None
    distance = _distance_km(float(latitude), float(longitude), float(home_latitude), float(home_longitude))
    bonus = 0.0
    if distance > max(0.0, float(home_radius_km)):
        if distance <= 200:
            bonus += near_bonus
        elif distance <= 1000:
            bonus += far_bonus
        elif foreign_country:
            bonus += foreign_bonus
        else:
            # 超過 1,000 km 但沒有可信國家資訊時不臆測跨國；保留遠行基本加分。
            bonus += far_bonus
    if foreign_country:
        bonus = max(bonus, foreign_bonus)
    if rare_location:
        bonus += rare_bonus
    return round(max(0.0, min(float(maximum), bonus)), 2), round(distance, 2)


DISTINCTIVE_SCORING_RULES = """【共通評分方法】
不要把普通照片全部放在 70～85 分。每一項都先從 50 分的「可用但普通」開始，只依畫面中能確認的證據加減分，並使用完整的 0～100 範圍：
- 0～19：幾乎不可用、嚴重失敗或完全沒有保留價值。
- 20～39：明顯較差，缺陷或低價值證據很多。
- 40～54：低於一般，勉強可用但沒有突出優點。
- 55～69：一般到不錯，有明確優點但仍常見。
- 70～82：明顯優秀，能具體指出兩項以上強項。
- 83～92：非常突出、少見且值得優先保留。
- 93～100：極少數代表作；沒有壓倒性證據不可使用。
相鄰照片若品質不同，分數至少拉開 5 分；不要因為不確定就一律給 75～80 分。

【回憶分 memory_score】
只評估值得回看與保留的程度，題材普通且沒有事件、互動或稀缺性時應落在 40～60 分，而不是自動給高分。

以下條件可疊加提高回憶分：
- 人物與關係：清楚且占比足夠的人臉、人物互動或合照，大幅提高評分。
- 事件性：生日、聚會、儀式、舞台或其他明確活動，提高評分。
- 稀缺性：難以重現、錯過便不再有的瞬間，大幅提高評分。
- 情緒強度：笑、哭、驚喜、擁抱、互動或強烈氛圍，提高評分。
- 資訊密度：畫面能清楚說明當時發生什麼，略微提高評分。
- 優美風景：壯麗自然風光、精緻或有秩序感的構圖，提高評分。
- 旅行意義：異地、地標或明確旅途情境，提高評分。
- 孩子、貓咪或其他寵物：通常具有較高個人回憶價值，先以約 75 分為基準，再依互動、事件與稀缺性調整。

以下條件降低回憶分：
- 模糊、失焦、殘影、主體被遮擋或曝光嚴重失敗，降低評分。
- 收據、帳單、廣告、螢幕截圖、測試圖片、隨手拍雜物或其他低價值記錄，應為 0～25 分，最高不可超過 39 分。

【美觀分 beauty_score】
只評估視覺品質，包括構圖、光線、清晰度、色彩與主體是否突出。人物、孩子、貓咪、寵物、旅行等主題本身不代表美觀分較高。

【技術品質分 technical_quality_score】
依對焦、曝光、動態模糊、雜訊、解析度與可用構圖評分，不因題材具有回憶價值而提高。

【情緒分 emotion_score】
依可觀察到的表情、互動、故事性與氛圍強度評分；不得臆測人物關係、身份、地點或未出現在畫面中的事件。

【輸出前自我校準】
輸出前重新檢查四項分數：若四項全落在 70～85，必須逐項找出具體證據；證據不足的項目回到 40～60。美觀、技術、情緒與回憶是獨立維度，不得只因題材討喜就全部給高分。"""


DEFAULT_SCORING_RULES = DISTINCTIVE_SCORING_RULES
