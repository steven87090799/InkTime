"""Behavior regressions for the 2026-10-04 audit, using only fixtures/stubs."""
import json
import pytest
from inktime.app.core.model_upload_guard import assert_upload_allowed, UploadNotAllowedError
from inktime.app.providers.base import ProviderResponse, Usage
from inktime.app.repositories.billable_operations import BillableOperationRepository
from inktime.app.services.jobs import JobService
from tests.conftest import create_admin, csrf, login
from tests.integration.test_jobs import add_photos


def test_estimates_follow_model_prices_and_preserve_unknown():
    service = JobService(None)
    cheap = dict(input_per_million=1, cached_input_per_million=0, output_per_million=2)
    expensive = dict(input_per_million=10, cached_input_per_million=0, output_per_million=20)
    a = service.estimate(10, "single", pricing=cheap)
    b = service.estimate(10, "single", pricing=expensive)
    assert b["average_cost"] == pytest.approx(a["average_cost"] * 10)
    assert service.estimate(10, "single")["average_cost"] is None
    assert service.estimate(10, "local")["average_cost"] == 0
    assert service.estimate(0, "single")["image_calls"] == 0


def test_upload_rechecks_current_privacy_and_job_state(app):
    database = app.extensions["inktime_database"]
    photo = add_photos(app, 1)[0]
    assert_upload_allowed(database.path, photo)
    with database.session() as connection:
        connection.execute("UPDATE photos SET never_upload=1 WHERE id=?", (photo,))
    with pytest.raises(UploadNotAllowedError) as error:
        assert_upload_allowed(database.path, photo)
    assert error.value.not_sent is True
    with database.session() as connection:
        connection.execute("UPDATE photos SET never_upload=0 WHERE id=?", (photo,))
    repository = app.extensions["inktime_job_repository"]
    job = repository.create_maintenance(kind="backup", name="fixture", settings={}, created_by="test")
    assert_upload_allowed(database.path, photo, job)
    repository.cancel(job)
    with pytest.raises(UploadNotAllowedError):
        assert_upload_allowed(database.path, photo, job)


def test_unauthenticated_critical_alerts_are_not_queried_or_rendered(app, client):
    create_admin(app)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("INSERT INTO job_errors(component,error_code,fingerprint,severity,message,first_seen_at,last_seen_at) VALUES ('private-component','VLM-UNRECONCILED','private-fingerprint','critical','PRIVATE-AUDIT-MARKER','PRIVATE-TIMESTAMP','PRIVATE-TIMESTAMP')")
    public = client.get("/login").get_data(as_text=True)
    assert "PRIVATE-TIMESTAMP" not in public and "需要優先處理" not in public
    login(client)
    private = client.get("/jobs").get_data(as_text=True)
    assert "PRIVATE-TIMESTAMP" in private


def test_no_release_still_returns_authenticated_device_configuration(app, client):
    devices = app.extensions["inktime_device_repository"]
    device, token = devices.create("empty-library", panel_profile="safe_4c")
    response = client.get("/api/device/v1/releases/latest", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["no_content"] is True and payload["files"] == []
    assert payload["device_config"]["config_version"] == devices.get(device)["config_version"]
    assert client.get("/api/device/v1/releases/latest").status_code == 401


def test_checkpoint_approval_is_admin_only_and_requires_explicit_cost_acceptance(app, client):
    create_admin(app)
    operations = BillableOperationRepository(app.extensions["inktime_database"])
    operation, _ = operations.begin("photo-sha", "request-plan")
    operations.save_response(operation, ProviderResponse("invalid JSON", Usage(20, 8)))
    url = f"/api/v1/paid-operations/{operation}/approve-resend"
    assert client.post(url, json={"reason": "fixture", "accept_additional_cost": True}).status_code in {302, 401, 403}
    login(client)
    headers = {"X-CSRF-Token": csrf(client)}
    assert client.post(url, json={"reason": "fixture"}, headers=headers).status_code == 400
    assert client.post(url, json={"reason": "fixture", "accept_additional_cost": True}, headers=headers).status_code == 200
    assert operations.begin("photo-sha", "request-plan")[0] != operation


def test_restore_guard_fences_requests_until_audited_approval(app, client):
    create_admin(app)
    login(client)
    database = app.extensions["inktime_database"]
    guard = database.path.with_suffix(database.path.suffix + ".paid-reconciliation-required")
    guard.write_text('{"requires_external_billing_reconciliation":true}')
    from inktime.app.services.budgets import BudgetExceeded
    budget = app.extensions["inktime_budget_service"]
    with pytest.raises(BudgetExceeded):
        budget.reserve("test", 0.01)
    response = client.post("/api/v1/paid-operations/restore-reconciled", json={"reason": "fixture billing reconciliation", "accept_additional_cost": True}, headers={"X-CSRF-Token": csrf(client)})
    assert response.status_code == 200 and not guard.exists()
    assert list(database.path.parent.glob(guard.name + ".approved-*"))
    budget.reserve("test", 0.01)


def test_stale_settings_form_cannot_overwrite_new_value(app, client):
    create_admin(app)
    login(client)
    settings = app.extensions["inktime_settings_repository"]
    key = "budget.daily_stop"
    original = next(row for row in settings.all() if row["key"] == key)["updated_at"]
    headers = {"X-CSRF-Token": csrf(client), "X-InkTime-Confirm-Risk": "true", "X-InkTime-Setting-Revisions": json.dumps({key: original})}
    assert client.post("/api/v1/settings", json={key: 11}, headers=headers).status_code == 200
    assert client.post("/api/v1/settings", json={key: 12}, headers=headers).status_code == 409
    assert settings.get(key) == 11


def test_stale_provider_form_does_not_change_key_or_model(app):
    actor = create_admin(app)
    providers = app.extensions["inktime_provider_repository"]
    payload = {"name": "fixture", "base_url": "https://provider.invalid/v1", "model": "fixture-a", "enabled": False, "api_key": "fixture-old-key"}
    provider = providers.save(payload, actor)
    revision = providers.get(provider)["updated_at"]
    providers.save({**payload, "id": provider, "model": "fixture-b", "api_key": "fixture-new-key", "expected_updated_at": revision}, actor)
    with pytest.raises(ValueError, match="CONFIG_CONFLICT"):
        providers.save({**payload, "id": provider, "model": "fixture-c", "expected_updated_at": revision}, actor)
    current = providers.get(provider, include_secret=True)
    assert current["model"] == "fixture-b" and current["api_key"] == "fixture-new-key"


def test_stale_pricing_snapshot_cannot_overwrite_saved_price(app):
    actor = create_admin(app)
    providers = app.extensions["inktime_provider_repository"]
    provider = providers.save({"name": "fixture", "base_url": "https://provider.invalid/v1", "enabled": False}, actor)
    price = {"model": "fixture-a", "input_per_million": 1, "cached_input_per_million": 0, "output_per_million": 2, "batch_multiplier": 0.5}
    providers.save_pricing(provider, {**price, "expected_pricing": {}})
    with pytest.raises(ValueError, match="CONFIG_CONFLICT"):
        providers.save_pricing(provider, {**price, "input_per_million": 10, "expected_pricing": {}})
    assert providers.pricing(provider)["fixture-a"]["input_per_million"] == 1


def test_paid_diagnostic_replay_returns_durable_result_and_rejects_changed_content(app, client, monkeypatch):
    from inktime.app.api import settings as settings_api
    actor = create_admin(app)
    login(client)
    provider = app.extensions["inktime_provider_repository"].save({"name": "fixture", "base_url": "https://provider.invalid/v1", "model": "fixture-model", "enabled": False}, actor)
    calls = []
    def diagnostic(_provider, **kwargs):
        calls.append(kwargs["level"])
        return {"ok": True, "level": kwargs["level"], "message": "fixture result"}
    monkeypatch.setattr(settings_api, "run_provider_contract", diagnostic)
    headers = {"X-CSRF-Token": csrf(client), "Idempotency-Key": "same-diagnostic-key"}
    url = f"/api/v1/providers/{provider}/test"
    first = client.post(url, json={"level": 2}, headers=headers)
    replay = client.post(url, json={"level": 2}, headers=headers)
    assert first.status_code == replay.status_code == 200
    assert first.get_json() == replay.get_json() and calls == [2]
    assert client.post(url, json={"level": 3}, headers=headers).status_code == 409
    assert calls == [2]


def test_provider_rechecks_consent_after_building_body_before_transport(app, tmp_path, monkeypatch):
    from inktime.app.providers.openai_compatible import OpenAICompatibleProvider
    database = app.extensions["inktime_database"]
    photo = add_photos(app, 1)[0]
    provider = OpenAICompatibleProvider(name="fixture", base_url="https://provider.invalid/v1", api_key="fixture")
    def body_builder(**kwargs):
        with database.session() as connection:
            connection.execute("UPDATE photos SET never_upload=1 WHERE id=?", (photo,))
        return {"model": "fixture-model", "messages": []}
    def forbidden_transport(*args, **kwargs):
        raise AssertionError("request must not reach transport")
    monkeypatch.setattr(provider, "build_analysis_request_body", body_builder)
    monkeypatch.setattr(provider, "_send", forbidden_transport)
    try:
        with pytest.raises(UploadNotAllowedError):
            provider.analyze(image_path=tmp_path / "unused.png", model="fixture-model", detail="high", stage="single", upload_guard={"database_path": str(database.path), "photo_id": photo})
    finally:
        provider.close()
