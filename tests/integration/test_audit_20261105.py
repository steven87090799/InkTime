"""Cross-request regressions from the November 5 audit; no live providers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading

from PIL import Image
import pytest

from inktime.app.providers.base import ProviderResponse, Usage
from inktime.app.repositories.billable_operations import BillableOperationRepository
from inktime.app.services.model_benchmark import BenchmarkError, ModelBenchmarkService
from inktime.app.services.openai_budget import OpenAIBudgetExceeded
from inktime.app.workers.runner import WorkerRunner
from tests.conftest import create_admin, csrf, login
from tests.integration.test_complementary_analysis import setup_dual, record_cost
from tests.unit.test_scoring import LabProvider


@pytest.mark.parametrize("account", [None, "openai", "direct-test"])
def test_native_benchmark_uses_registered_account_and_refuses_exhausted_budget(app, monkeypatch, account):
    setup_dual(app, **{"openai_budget.cycle_limit": 1})
    record_cost(app, 1)
    budgets = app.extensions["inktime_budget_service"]
    service = ModelBenchmarkService(budgets=budgets, provider_id=account)
    axis = service.build_axes(provider="openai", models=["gpt-6-luna"], image_sides=[512],
                              prompt_profiles=["default"], variants=[False], reasoning_efforts=["none"])[0]
    provider = service._provider(axis, api_key="mock-key", base_url="https://api.openai.com/v1")
    calls = []
    monkeypatch.setattr(provider, "analyze", lambda **kwargs: calls.append(kwargs))
    try:
        with pytest.raises(OpenAIBudgetExceeded if account == "direct-test" else BenchmarkError):
            service._paid_call(provider, "analyze", model=axis.model, max_tokens=100)
        assert calls == []
        assert provider.provider_id == (account or "openai")
    finally:
        provider.close()


def test_native_diagnostic_receipt_and_reservation_use_same_account(app, monkeypatch):
    setup_dual(app)
    budgets = app.extensions["inktime_budget_service"]
    service = ModelBenchmarkService(budgets=budgets, provider_id="direct-test")
    axis = service.build_axes(provider="openai", models=["gpt-6-luna"], image_sides=[512],
                              prompt_profiles=["default"], variants=[False], reasoning_efforts=["none"])[0]
    provider = service._provider(axis, api_key="mock-key", base_url="https://api.openai.com/v1")
    monkeypatch.setattr(provider, "analyze", lambda **kwargs: ProviderResponse(
        "{}", Usage(100, 10, provider_reported_cost=.01), request_id="benchmark-receipt"))
    try:
        service._paid_call(provider, "analyze", model=axis.model, max_tokens=100)
    finally:
        provider.close()
    with budgets.database.session() as connection:
        assert connection.execute("SELECT provider_id,provider_kind,state FROM budget_reservations").fetchone()[:] == (
            "direct-test", "openai", "released")
        assert connection.execute("SELECT provider_id FROM openai_budget_receipts").fetchone()[0] == "direct-test"


@pytest.mark.parametrize("enabled", [True, False])
def test_repair_confirmation_restores_only_enabled_offline_prepare_deadlines(app, enabled):
    devices = app.extensions["inktime_device_repository"]
    service = app.extensions["inktime_device_pairing_service"]
    device_id, _ = devices.create("repair", auth_mode="automatic", enabled=enabled,
                                  delivery_mode="inktime_offline_schedule", offline_prefetch_allowed=True)
    with devices.database.transaction() as connection:
        connection.execute("UPDATE devices SET next_offline_prepare_at=NULL,offline_schedule_capability_state='legacy_ambiguous' WHERE id=?", (device_id,))
        service._update_confirmed_device(connection, device_id=device_id,
            envelope={"credential_version": 2, "device_secret": "test-confirmation-secret", "offline_schedule_max_slots": 24},
            now=datetime.now(timezone.utc))
    rows, _ = app.extensions["inktime_offline_schedule_repository"].due_prefetch_devices()
    assert (device_id in {row["id"] for row in rows}) is enabled


def test_name_edit_increments_form_version_and_rejects_stale_form(app):
    devices = app.extensions["inktime_device_repository"]
    device_id, _ = devices.create("original")
    device = devices.get(device_id)
    values = dict(enabled=True, timezone_name=device["timezone"], schedule=device["schedule"],
                  rotation=device["rotation"], panel_profile=device["panel_profile"])
    assert devices.update(device_id, name="first editor", expected_config_version=device["config_version"], **values)
    assert devices.get(device_id)["config_version"] == device["config_version"] + 1
    with pytest.raises(ValueError, match="其他管理員"):
        devices.update(device_id, name="stale editor", expected_config_version=device["config_version"], **values)
    assert devices.get(device_id)["name"] == "first editor"


def test_second_manual_backup_creates_new_job_and_archive_after_data_changes(app, client):
    create_admin(app)
    login(client)
    headers = {"X-CSRF-Token": csrf(client)}
    first = client.post("/api/v1/backups", headers=headers).get_json()
    duplicate = client.post("/api/v1/backups", headers=headers).get_json()
    assert first["reused"] is False and duplicate["reused"] is True
    assert first["job_id"] == duplicate["job_id"]
    runner = WorkerRunner(app, isolate_local=False)
    runner.run_once()
    jobs = app.extensions["inktime_job_repository"]
    assert jobs.get(first["job_id"])["status"] == "completed"
    backups = app.extensions["inktime_backup_service"]
    first_paths = set(backups.backup_dir.glob("*.zip"))
    app.extensions["inktime_settings_repository"].update("render.quantity", 2, changed_by="test", source_ip="test")
    second = client.post("/api/v1/backups", headers=headers).get_json()
    assert second["job_id"] != first["job_id"] and second["reused"] is False
    runner.run_once()
    assert jobs.get(second["job_id"])["status"] == "completed"
    assert len(set(backups.backup_dir.glob("*.zip")) - first_paths) == 1
    scheduled = jobs.create_maintenance(kind="backup", name="scheduled", settings={}, created_by="test", dedupe_key="backup:day")
    jobs.transition(scheduled, {"pending"}, "completed", "test_completed")
    assert jobs.create_maintenance(kind="backup", name="scheduled", settings={}, created_by="test", dedupe_key="backup:day") == scheduled


def test_scoring_request_blocks_resend_approval_until_its_receipt_is_durable(app, tmp_path, monkeypatch):
    image = tmp_path / "scoring.jpg"
    Image.new("RGB", (64, 64), "orange").save(image)
    service = app.extensions["inktime_scoring_lab_service"]
    entered, release = threading.Event(), threading.Event()
    class BlockingProvider(LabProvider):
        def analyze(self, **kwargs):
            entered.set()
            assert release.wait(5)
            return super().analyze(**kwargs)
    monkeypatch.setattr(service.providers, "build_router", lambda *args, **kwargs: BlockingProvider())
    operations = BillableOperationRepository(service.usage.database)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(service.analyze, image)
        try:
            assert entered.wait(5)
            with service.usage.database.session() as connection:
                operation_id = connection.execute("SELECT id FROM billable_operations WHERE state='started'").fetchone()[0]
            with pytest.raises(ValueError, match="處理中"):
                operations.approve_resend(operation_id, reason="concurrent approval")
        finally:
            release.set()
        assert pending.result(timeout=5)


def test_provider_test_blocks_approval_during_its_external_call(app, client, monkeypatch):
    import inktime.app.api.settings as settings_module
    setup_dual(app)
    login(client)
    operations = BillableOperationRepository(app.extensions["inktime_database"])
    calls = []
    def contract(*args, **kwargs):
        with operations.database.session() as connection:
            operation_id = connection.execute("SELECT id FROM billable_operations WHERE state='started'").fetchone()[0]
        with pytest.raises(ValueError, match="處理中"):
            operations.approve_resend(operation_id, reason="approval while provider test is active")
        calls.append(True)
        return {"ok": True}
    monkeypatch.setattr(settings_module, "run_provider_contract", contract)
    response = client.post("/api/v1/providers/direct-test/test", json={"level": 2},
                           headers={"X-CSRF-Token": csrf(client)})
    assert response.status_code == 200 and calls == [True]


@pytest.mark.parametrize("known_cost", [True, False])
@pytest.mark.parametrize("prefixed", [True, False])
def test_reservation_reconciliation_uses_operation_evidence_for_all_key_formats(app, known_cost, prefixed):
    database = app.extensions["inktime_database"]
    budgets = app.extensions["inktime_budget_service"]
    operations = BillableOperationRepository(database)
    operation_id, _ = operations.begin("scoring-content", "scoring-plan")
    reservation_id = ("vision:" if prefixed else "") + operation_id
    budgets.reserve(reservation_id, .25)
    operations.save_response(operation_id, ProviderResponse("{}", Usage(100, 10)))
    app.extensions["inktime_usage_repository"].record(
        provider="test", model="test", job_id=None, photo_id=None, request_type="scoring_test_vision",
        input_tokens=100, output_tokens=10, cached_tokens=0, estimated_cost=.01 if known_cost else None,
        actual_cost=None, started_at=datetime.now(timezone.utc).isoformat(), latency_ms=1,
        status="completed", operation_id=operation_id, cost_source="estimated" if known_cost else "unknown")
    with database.transaction() as connection:
        connection.execute("UPDATE budget_reservations SET created_at='2000-01-01T00:00:00+00:00'")
    assert budgets.expire_stale_reservations() == int(known_cost)
    with database.session() as connection:
        assert connection.execute("SELECT state FROM budget_reservations WHERE id=?", (reservation_id,)).fetchone()[0] == (
            "released" if known_cost else "active")
