from __future__ import annotations

from inktime.app.domain.analysis.execution_mode import execution_mode
from tests.conftest import create_admin, csrf, login


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
        strategy="smart_two_stage",
        provider_route=[],
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


def test_disabled_rejects_analysis_job_before_a_plan_or_job_item_is_created(client, app):
    create_admin(app)
    login(client)
    settings = app.extensions["inktime_settings_repository"]
    settings.update("analysis.execution_mode", "disabled", changed_by="test", source_ip="test")
    response = client.post(
        "/api/v1/jobs", json={"strategy": "high_quality"}, headers={"X-CSRF-Token": csrf(client)}
    )
    assert response.status_code == 409
    assert response.json["error_code"] == "ANALYSIS-DISABLED"
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE kind='analysis'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM job_items").fetchone()[0] == 0


def test_model_job_requires_a_configured_usable_provider_but_local_job_does_not(client, app):
    create_admin(app)
    login(client)
    app.extensions["inktime_settings_repository"].update(
        "analysis.execution_mode", "automatic_ai", changed_by="test", source_ip="test"
    )
    headers = {"X-CSRF-Token": csrf(client)}

    blocked = client.post(
        "/api/v1/jobs",
        json={"strategy": "single", "photo_ids": []},
        headers=headers,
    )
    local = client.post(
        "/api/v1/jobs",
        json={"strategy": "local", "photo_ids": []},
        headers=headers,
    )

    assert blocked.status_code == 409
    assert blocked.json["error_code"] == "VLM-001"
    assert "Vision Provider" in blocked.json["message"]
    assert local.status_code == 201


def test_analysis_idempotency_conflict_has_stable_api_error_code(client, app):
    create_admin(app)
    login(client)
    app.extensions["inktime_settings_repository"].update(
        "analysis.execution_mode", "automatic_ai", changed_by="test", source_ip="test"
    )
    headers = {"X-CSRF-Token": csrf(client), "Idempotency-Key": "analysis-conflict-key"}
    first = client.post(
        "/api/v1/jobs",
        json={"name": "first", "strategy": "local", "photo_ids": []},
        headers=headers,
    )
    second = client.post(
        "/api/v1/jobs",
        json={"name": "different", "strategy": "local", "photo_ids": []},
        headers=headers,
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json["error_code"] == "IDEMPOTENCY_CONFLICT"
