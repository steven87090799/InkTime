from __future__ import annotations

from inktime.app.domain.analysis.execution_mode import execution_mode


def test_new_install_defaults_to_local_only_without_provider_route(app, monkeypatch):
    settings = app.extensions["inktime_settings_repository"]
    providers = app.extensions["inktime_provider_service"]
    assert execution_mode(settings) == "local_only"
    called = 0

    def route_snapshot():
        nonlocal called
        called += 1
        return []

    monkeypatch.setattr(providers, "route_snapshot", route_snapshot)
    plan = app.extensions["inktime_analysis_service"].build_plan(
        strategy="smart_two_stage", provider_route=[],
        scoring_profile=dict(app.extensions["inktime_scoring_repository"].current()),
    )
    assert plan["ai_execution_policy"]["execution_mode"] == "local_only"
    assert plan["provider_route"] == []
    assert called == 0


def test_legacy_ai_mode_update_maps_to_safe_execution_mode(app):
    settings = app.extensions["inktime_settings_repository"]
    settings.update("analysis.ai_mode", "on_demand", changed_by="test", source_ip="127.0.0.1")
    assert settings.get("analysis.execution_mode") == "local_with_manual_ai"
    settings.update("analysis.ai_mode", "eligible", changed_by="test", source_ip="127.0.0.1")
    assert settings.get("analysis.execution_mode") == "automatic_ai"
