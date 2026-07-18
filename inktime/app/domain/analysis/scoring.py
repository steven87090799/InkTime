from __future__ import annotations

from typing import Mapping


DEFAULT_RANKING_WEIGHTS = {
    "memory": 50.0,
    "beauty": 20.0,
    "technical_quality": 10.0,
    "emotion": 20.0,
}
DEFAULT_FAVORITE_BONUS = 5.0


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


DEFAULT_SCORING_RULES = """【回憶分 memory_score】
先判斷照片所屬區間，再依加分與扣分條件微調，分數範圍為 0～100：
- 垃圾、隨手拍或無意義記錄：40 分以下；常見為 0～25 分，勉強可辨識但沒有故事也不可超過 39 分。
- 稍有回憶價值：以 65 分為中心，通常落在 58～70 分。
- 不錯的回憶價值：以 75 分為中心，通常落在 69～82 分。
- 特別精彩、強烈值得珍藏：以 85 分為中心，通常落在 79～96 分。

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
依可觀察到的表情、互動、故事性與氛圍強度評分；不得臆測人物關係、身份、地點或未出現在畫面中的事件。"""
