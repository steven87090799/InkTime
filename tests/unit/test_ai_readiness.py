from inktime.app.web.ai_readiness import ai_readiness_snapshot


class FakeSettings:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeProviderRepository:
    def __init__(self, rows):
        self.rows = rows

    def list(self):
        return self.rows


class FakeProviderService:
    def __init__(self, routes):
        self.routes = routes

    def usable_route_snapshot(self):
        return self.routes


def test_readiness_explains_every_required_ai_gate():
    snapshot = ai_readiness_snapshot(
        FakeSettings(
            {
                "analysis.execution_mode": "local_only",
                "model.analysis_model": "vision-model",
            }
        ),
        FakeProviderRepository(
            [
                {
                    "id": "provider-1",
                    "name": "Incomplete",
                    "kind": "openai",
                    "enabled": True,
                    "supports_vision": True,
                    "base_url": "https://example.test/v1",
                    "api_key_configured": False,
                }
            ]
        ),
        FakeProviderService([]),
    )

    assert snapshot["ready"] is False
    assert snapshot["ready_count"] == 1
    assert snapshot["required_count"] == 3
    assert snapshot["checks"][0]["current"] == "僅使用本機選片"
    assert snapshot["checks"][0]["action_url"] == "/settings?search=分析執行模式"
    assert snapshot["provider_details"][0]["issues"] == ("尚未設定 API Key",)


def test_readiness_is_complete_only_with_automatic_ai_model_and_usable_provider():
    snapshot = ai_readiness_snapshot(
        FakeSettings(
            {
                "analysis.execution_mode": "automatic_ai",
                "model.analysis_model": "vision-model",
            }
        ),
        FakeProviderRepository(
            [
                {
                    "id": "provider-1",
                    "name": "Ready",
                    "kind": "openai",
                    "enabled": True,
                    "supports_vision": True,
                    "base_url": "https://example.test/v1",
                    "api_key_configured": True,
                }
            ]
        ),
        FakeProviderService(
            [{"provider_id": "provider-1", "display_name": "Ready", "model": "fixed-vision"}]
        ),
    )

    assert snapshot["ready"] is True
    assert snapshot["ready_count"] == 3
    assert snapshot["provider_details"][0]["ready"] is True
    assert snapshot["checks"][2]["current"] == "Ready：fixed-vision"
