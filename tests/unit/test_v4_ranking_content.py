import pytest
from inktime.app.domain.analysis.scoring import (
    calculate_ranking_score,
    ranking_components,
    library_rarity_adjustment,
    DEFAULT_RANKING_WEIGHTS,
    calculate_distinguishing_score,
)
from inktime.app.domain.analysis.content_filter import evaluate_content_filter, CONTENT_FILTER_SWITCHES
from inktime.app.domain.analysis.schema import validate_analysis_result
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider
from inktime.app.domain.analysis.scoring import DEFAULT_SCORING_RULES
from inktime.app.services.analysis import FULL_ANALYSIS_TOKEN_CAP
from tests.unit.test_analysis_schema import valid_result


@pytest.mark.parametrize("level,bonus", list(enumerate((0, 2, 5, 9, 14))))
def test_special_and_ranking_formula(level, bonus):
    value = valid_result(memory_score=80, visual_score=60, special_level=level)
    value["local_quality_score"] = 40
    assert calculate_ranking_score(value, DEFAULT_RANKING_WEIGHTS) == 65 + bonus


def test_favorite_and_rarity_are_levels_and_clamped():
    value = valid_result(special_level=2) | {"local_quality_score": 80}
    parts = ranking_components(value, favorite=True, rarity_adjustment=1)
    assert parts["effective_special_level"] == 4 and parts["special_bonus"] == 14
    assert (
        calculate_ranking_score(
            valid_result(memory_score=100, visual_score=100, special_level=4) | {"local_quality_score": 100}
        )
        == 100
    )


def test_rarity_is_local_and_requires_enough_peers():
    ordinary = valid_result(types=["風景"], special_codes=[], people_count=0)
    rare = valid_result(types=["活動"], special_codes=["ceremony"], people_count=20)
    assert library_rarity_adjustment(rare, [ordinary] * 19) == 0
    assert library_rarity_adjustment(rare, [ordinary] * 20) == 1
    assert library_rarity_adjustment(ordinary, [ordinary] * 20) == 0


@pytest.mark.parametrize("code", list(CONTENT_FILTER_SWITCHES))
@pytest.mark.parametrize(
    "confidence,enabled,excluded", [(0.95, True, True), (0.8, True, False), (0.95, False, False)]
)
def test_settings_control_each_content_threshold(code, confidence, enabled, excluded):
    value = validate_analysis_result(
        valid_result(content_filter={"exclude_code": code, "confidence": confidence})
    )
    policy = evaluate_content_filter(value["content_filter"], {CONTENT_FILTER_SWITCHES[code]: enabled})
    assert (policy["decision"] == "auto_excluded") == excluded
    assert value["visual_orientation"]["rotation_cw"] == 0


@pytest.mark.parametrize("code", ["none", "uncertain"])
def test_ordinary_single_person_photos_have_no_gender_heuristic(code):
    value = valid_result(
        types=["人物", "旅行"], people_count=1, content_filter={"exclude_code": code, "confidence": 1}
    )
    assert evaluate_content_filter(value["content_filter"])["decision"] == "pass"


def test_prompt_guardrails_and_no_duplicate_default_rubric():
    provider = OpenAICompatibleProvider(name="test", base_url="https://example.com/v1", api_key="test")
    prompt = provider.system_prompt
    assert prompt.count(DEFAULT_SCORING_RULES) == 1
    for text in (
        "女性+單人絕不直接成立",
        "普通自拍",
        "畢業",
        "不推論真實性別身份",
        "排除內容也不得省略方向",
        "夜市",
    ):
        assert text in prompt
    for old in ("technical_quality_score", "emotion_score", "memory_grade", "reason_codes"):
        assert old not in prompt
    assert 1000 <= FULL_ANALYSIS_TOKEN_CAP < 2048
    provider.close()


def test_percentile_then_e6_contract():
    distinguishing, percentile = calculate_distinguishing_score(72, [70, 71, 72, 73, 74])
    assert percentile == 50
    assert distinguishing == 57.7
    assert distinguishing * 0.8 + 90 * 0.2 == pytest.approx(64.16)
