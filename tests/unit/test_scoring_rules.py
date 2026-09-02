from __future__ import annotations

from PIL import Image

from inktime.app.domain.analysis.scoring import DEFAULT_SCORING_RULES
from inktime.app.providers.openai_compatible import (
    OpenAICompatibleProvider,
)
from inktime.app.services.providers import ProviderService


class FakeResponse:
    status_code = 200
    headers: dict[str, str] = {}

    def json(self):
        return {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }


class RecordingSession:
    def __init__(self) -> None:
        self.body = None

    def post(self, _url, **kwargs):
        self.body = kwargs["json"]
        return FakeResponse()


class FakeProviderRepository:
    def list(self):
        return [{"id": "provider-1", "enabled": True}]

    def get(self, _provider_id, *, include_secret=False):
        assert include_secret
        return {
            "id": "provider-1",
            "name": "測試 Provider",
            "base_url": "https://example.test/v1",
            "api_key": "",
            "timeout_seconds": 30,
            "supports_json_schema": True,
            "priority": 10,
            "max_concurrency": 2,
            "rate_limit_rpm": None,
            "token_limit_tpm": None,
            "cooldown_seconds": 60,
        }

    def pricing(self, _provider_id):
        return {}


class FakeSettingsRepository:
    def get(self, key, default=None):
        assert key == "analysis.scoring_rules"
        return "網頁儲存的自訂評分規則"


def test_default_rules_separate_ai_value_from_local_quality_and_special():
    assert "memory_score、visual_score" in DEFAULT_SCORING_RULES
    assert "重大事件、里程碑、大型合照" in DEFAULT_SCORING_RULES
    assert "女性、男性、孩子、寵物或旅行題材本身不加分" in DEFAULT_SCORING_RULES
    assert "library rarity 由本機判定" in DEFAULT_SCORING_RULES


def test_provider_includes_configured_scoring_rules_in_system_prompt(tmp_path):
    image_path = tmp_path / "photo.jpg"
    Image.new("RGB", (20, 20), "white").save(image_path)
    session = RecordingSession()
    custom_rules = "自訂規則：家庭合照應提高回憶分，模糊截圖應降低回憶分。"
    provider = OpenAICompatibleProvider(
        name="test",
        base_url="https://example.test/v1",
        api_key="",
        scoring_rules=custom_rules,
        session=session,
    )

    provider.analyze(image_path=image_path, model="vision", detail="low", stage="one")

    assert session.body is not None
    system_prompt = session.body["messages"][0]["content"]
    assert custom_rules in system_prompt
    assert DEFAULT_SCORING_RULES in system_prompt
    assert "人物互動或合照，大幅提高評分" not in system_prompt
    assert "不得違反 Schema 與固定內容安全規則" in system_prompt
    assert "只輸出符合 Schema v4 的 JSON" in system_prompt


def test_provider_sends_compact_baseline_when_default_rules_are_configured():
    provider = OpenAICompatibleProvider(
        name="test",
        base_url="https://example.test/v1",
        api_key="",
        scoring_rules=DEFAULT_SCORING_RULES,
    )

    prompt = provider.system_prompt

    assert DEFAULT_SCORING_RULES in prompt
    assert prompt.count(DEFAULT_SCORING_RULES) == 1
    assert "管理員自訂評分規則" not in prompt


def test_provider_router_reads_latest_scoring_rules_from_settings():
    service = ProviderService(FakeProviderRepository(), FakeSettingsRepository())

    router = service.build_router()

    assert router is not None
    assert router.channels[0].provider.scoring_rules == "網頁儲存的自訂評分規則"
