from __future__ import annotations

import pytest

from tests.conftest import create_admin, csrf, login
from tests.integration.test_photo_display_history import _photo, _setting


def _trace(app, root):
    _photo(app, root, "failure-photo")
    return app.extensions["inktime_ai_trace_repository"].start_run(
        job_id=None, photo_id="failure-photo", stage="vision", prompt_version="test",
        analysis_fingerprint=None, vision_request_fingerprint="failure-test",
    )


@pytest.mark.parametrize("status", ["FAILED", "TIMEOUT", "AMBIGUOUS"])
def test_model_failure_is_durable_unread_and_idempotent(client, app, tmp_path, status):
    trace_id = _trace(app, tmp_path / "failure")
    traces = app.extensions["inktime_ai_trace_repository"]
    for _ in range(2):
        traces.complete_run(trace_id, status=status, error_code="AI-001", error_message="private provider payload")
    service = app.extensions["inktime_notification_service"]
    assert service.unread_analysis()["count"] == 1
    notification = service.list()[0]
    assert notification["kind"] == "analysis_failure"
    assert "private provider payload" not in notification["details_json"]
    create_admin(app)
    assert client.get("/api/v1/notifications/unread-analysis").status_code == 401
    login(client)
    assert client.get("/api/v1/notifications/unread-analysis").get_json()["count"] == 1
    page = client.get("/notifications")
    assert page.status_code == 200
    assert trace_id.encode() in page.data
    assert b'analysis-failure-alert' in client.get("/dashboard").data
    endpoint = f"/notifications/{notification['id']}/read"
    assert client.post(endpoint).status_code == 403
    assert service.unread_analysis()["count"] == 1
    assert client.post(endpoint, data={"csrf_token": csrf(client)}).status_code == 302
    assert service.unread_analysis()["count"] == 0
    assert len(service.list()) == 1


def test_batch_item_failure_without_trace_notifies_and_success_does_not(app, tmp_path):
    _photo(app, tmp_path / "batch", "batch-photo")
    jobs = app.extensions["inktime_job_repository"]
    job_id = jobs.create(
        kind="analysis_batch", name="Batch", strategy="best", settings={},
        photo_ids=["batch-photo"], created_by="test",
    )
    item_id = jobs.list_items(job_id)[0]["id"]
    assert jobs.fail_batch_item(job_id, item_id, "BATCH-TEST", "invalid model JSON")
    assert not jobs.fail_batch_item(job_id, item_id, "BATCH-TEST", "duplicate")
    service = app.extensions["inktime_notification_service"]
    assert service.unread_analysis()["count"] == 1
    trace_id = _trace(app, tmp_path / "success")
    app.extensions["inktime_ai_trace_repository"].complete_run(trace_id, status="SUCCESS")
    assert service.unread_analysis()["count"] == 1


def test_failure_outbox_rolls_back_with_failed_transaction(app, tmp_path):
    trace_id = _trace(app, tmp_path / "rollback")
    with pytest.raises(RuntimeError, match="rollback"):
        with app.extensions["inktime_database"].transaction() as connection:
            connection.execute(
                "UPDATE ai_trace_runs SET status='FAILED',completed_at='2026-10-22T00:00:00+00:00' WHERE trace_id=?",
                (trace_id,),
            )
            raise RuntimeError("rollback")
    assert app.extensions["inktime_notification_service"].unread_analysis()["count"] == 0


def test_analysis_failure_uses_existing_webhook_outbox_without_provider_secrets(app, tmp_path):
    _setting(app, "notification.webhook_enabled", True)
    _setting(app, "notification.webhook_url", "https://hooks.example.test/inktime")
    trace_id = _trace(app, tmp_path / "webhook")
    app.extensions["inktime_ai_trace_repository"].complete_run(trace_id, status="FAILED", error_code="AI-001")
    service = app.extensions["inktime_notification_service"]
    calls = []

    class Response:
        status_code = 204

    class Transport:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    service.session = Transport()
    assert service.list()[0]["webhook_status"] == "pending"
    assert service.deliver_pending()["delivered"] == 1
    payload = calls[0][1]["json"]
    assert payload["kind"] == "analysis_failure"
    assert payload["device"] is None
    assert payload["details"]["trace_id"] == trace_id
