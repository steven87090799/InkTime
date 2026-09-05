from __future__ import annotations

from typing import Mapping


DEFAULT_RANKING_WEIGHTS = {"memory": 67.0, "visual": 33.0, "local_quality": 0.0}
DEFAULT_FAVORITE_BONUS = 1
RANKING_RULE_VERSION = "ranking-v5-ai-first"
SPECIAL_BONUSES = (0, 2, 5, 9, 14)

# ``score_kind`` is an explicit data contract.  A provider name alone is not
# sufficient to tell consumers whether a row contains model semantics or only
# local image-quality evidence (and historical rows may have neither reliably).
SEMANTIC_SCORE_KIND = "semantic"
LOCAL_QUALITY_SCORE_KIND = "local_quality"
LEGACY_SCORE_KIND = "legacy"
SCORE_KINDS = frozenset(
    {SEMANTIC_SCORE_KIND, LOCAL_QUALITY_SCORE_KIND, LEGACY_SCORE_KIND}
)
LOCAL_ANALYSIS_STAGES = frozenset({"local", "local_fallback", "prefilter"})
LOCAL_ANALYSIS_PROVIDERS = frozenset(
    {"local", "local-prefilter", "local-quality-v3", "virtual-display-local"}
)


def preferred_analysis_order_sql(alias: str = "") -> str:
    """Prefer semantic analysis over newer local-only evidence in read models.

    Scanner and policy runs may append local evidence after a model result.  A
    source-aware order keeps that evidence available as history without making
    it hide a completed semantic result from selection and rendering readers.
    ``alias`` is a server-controlled SQL alias, never user input.
    """

    prefix = f"{alias}." if alias else ""
    return (
        f"CASE WHEN lower(COALESCE({prefix}score_kind,''))='{SEMANTIC_SCORE_KIND}' THEN 0 "
        f"WHEN lower(COALESCE({prefix}provider,'')) IN "
        "('local','local-prefilter','local-quality-v3','virtual-display-local') "
        f"OR lower(COALESCE({prefix}stage,'')) IN ('local','local_fallback','prefilter') THEN 1 ELSE 2 END,"
        f"{prefix}created_at DESC,{prefix}id DESC"
    )


def resolve_score_kind(
    score_kind: str | None = None,
    *,
    provider: str | None = None,
    stage: str | None = None,
) -> str:
    """Resolve a persisted score source while keeping old call sites safe.

    Explicit values always win.  For pre-migration callers, known local
    stages/providers are local quality; a named non-local provider is treated
    as semantic because that is how the historical AI write path identified a
    successful model result.  Empty or ambiguous identities remain legacy.
    """

    requested = str(score_kind or "").strip().casefold()
    if requested in SCORE_KINDS:
        return requested
    provider_name = str(provider or "").strip().casefold()
    stage_name = str(stage or "").strip().casefold()
    if provider_name in LOCAL_ANALYSIS_PROVIDERS or stage_name in LOCAL_ANALYSIS_STAGES:
        return LOCAL_QUALITY_SCORE_KIND
    if provider_name and provider_name != "inherited":
        return SEMANTIC_SCORE_KIND
    return LEGACY_SCORE_KIND


def score_band(score: float) -> str:
    marker = float(score)
    if marker >= 90:
        return "精選"
    if marker >= 75:
        return "推薦"
    if marker >= 40:
        return "一般"
    return "較弱"


def validate_ranking_weights(weights: Mapping[str, float]) -> dict[str, float]:
    if dict(weights) != DEFAULT_RANKING_WEIGHTS:
        raise ValueError("排序權重固定為 AI 回憶 67%、AI 視覺 33%；本機品質只作門檻")
    return dict(DEFAULT_RANKING_WEIGHTS)


def ranking_components(analysis: Mapping, *, favorite: bool = False) -> dict:
    """Compose the AI ranking after the local quality gate has passed.

    Local measurements remain persisted as evidence, but never increase or
    decrease the rank. This prevents a technically crisp but uninteresting
    photo from outranking a model-selected photo, and prevents missing local
    score values from silently disadvantaging an otherwise valid AI result.
    """
    base = (
        float(analysis["memory_score"]) * 0.67
        + float(analysis["visual_score"]) * 0.33
    )
    base = round(base, 2)
    effective = max(0, min(4, int(analysis["special_level"]) + int(favorite)))
    raw = round(max(0.0, min(100.0, base + SPECIAL_BONUSES[effective])), 2)
    return {
        "base_ranking_score": round(base, 2),
        "effective_special_level": effective,
        "library_rarity_adjustment": 0,
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
) -> float:
    if weights is not None:
        validate_ranking_weights(weights)
    return ranking_components(analysis, favorite=favorite)["raw_ranking_score"]


LEGACY_DEFAULT_SCORING_RULES = """memory_score、visual_score 各為 0～100 數字，普通照片約 40～60，使用完整範圍。
memory 評一般情況下值得回看的程度：人物、互動、活動、日常紀錄與故事資訊量；不猜對使用者本人的重要性。重大事件、里程碑、大型合照與極罕見瞬間主要放 special_level，避免重複加分。
visual 只評構圖、光線、主體突出、色彩明暗、平衡及整體吸引力；女性、男性、孩子、寵物或旅行題材本身不加分。模糊、曝光、解析度與技術品質由本機計算。
special_level：0 普通；1 稍有特色；2 明確活動、合照、旅行紀錄或難得互動；3 重要典禮、舞台、大型合照或難重現事件；4 極罕見人生里程碑或無法重現的重要紀錄。非常保守使用 3、4，只填 level，不輸出 bonus。
special_codes 最多 2 個。group_photo 必須整群人為主體、共同面向鏡頭或明顯共同合影；夜市、觀眾、車站及街道人潮不算合照。people_count 只依可見人數。不宣稱照片在使用者照片庫少見，library rarity 由本機判定。"""
DEFAULT_SCORING_RULES = """memory_score：普通照片約 40～60，使用完整範圍。依人物、互動、活動、日常紀錄與故事資訊量判斷一般回看價值。
visual_score：普通照片約 40～60。依構圖、光線、主體突出、色彩明暗、平衡及整體吸引力評分；女性、男性、孩子、寵物或旅行題材本身不加分。
special_level 參考：0 普通；1 稍有特色；2 明確活動、合照、旅行紀錄或難得互動；3 重要典禮、舞台、大型合照或難重現事件；4 極罕見人生里程碑或無法重現的重要紀錄。非常保守使用 3、4。"""
DISTINCTIVE_SCORING_RULES = DEFAULT_SCORING_RULES


def validate_scoring_rules(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 12000:
        raise ValueError("評分參考必須為 1 到 12000 個字元")
    return value.strip()


def normalize_scoring_rules(value: object) -> str:
    """Replace the old bundled default only; preserve every administrator edit."""
    clean = validate_scoring_rules(value)
    return DEFAULT_SCORING_RULES if clean == LEGACY_DEFAULT_SCORING_RULES else clean
