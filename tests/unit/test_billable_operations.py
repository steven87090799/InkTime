import pytest

from inktime.app.db import Database, migrate
from inktime.app.providers.base import ProviderResponse, Usage
from inktime.app.repositories.billable_operations import BillableOperationRepository, UnreconciledOperationError
from inktime.app.repositories.usage import UsageRepository


def test_unknown_operation_survives_restart_and_provider_plan_change(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    migrate(database)
    repository = BillableOperationRepository(database)
    operation_id, response = repository.begin("sha", "provider-a-plan")
    assert response is None
    restarted = BillableOperationRepository(Database(database.path))
    with pytest.raises(UnreconciledOperationError):
        restarted.begin("sha", "provider-b-plan")
    repository.finish(operation_id, not_sent=True)
    assert restarted.begin("sha", "provider-b-plan")[1] is None


def test_saved_invalid_response_resumes_without_second_charge(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    migrate(database)
    repository = BillableOperationRepository(database)
    operation_id, _ = repository.begin("sha", "plan")
    response = ProviderResponse("invalid JSON", Usage(12, 7), request_id="provider-request")
    repository.save_response(operation_id, response)
    restarted = BillableOperationRepository(Database(database.path))
    resumed_id, resumed = restarted.begin("sha", "plan")
    assert resumed_id == operation_id
    assert resumed == response
    usage = UsageRepository(database)
    values = dict(provider="test", model="test", job_id=None, photo_id=None,
                  request_type="vision", input_tokens=12, output_tokens=7, cached_tokens=0,
                  estimated_cost=0.01, actual_cost=None, started_at="2026-09-16T00:00:00+00:00",
                  latency_ms=1, status="completed", operation_id=operation_id)
    assert usage.record(**values) == usage.record(**values)
    with database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 1


def test_resend_requires_explicit_audited_approval(tmp_path):
    database = Database(tmp_path / "approval.sqlite3")
    migrate(database)
    repository = BillableOperationRepository(database)
    operation_id, _ = repository.begin("sha", "plan")
    with pytest.raises(ValueError, match="原因"):
        repository.approve_resend(operation_id, reason="")
    with pytest.raises(UnreconciledOperationError):
        repository.check_retry("sha", "other-plan")
    repository.approve_resend(operation_id, reason="operator accepted duplicate billing risk")
    assert repository.begin("sha", "other-plan")[0] != operation_id
    with database.session() as connection:
        row = connection.execute("SELECT state,resolution_note FROM billable_operations WHERE id=?", (operation_id,)).fetchone()
        assert row["state"] == "approved"
        assert row["resolution_note"]
