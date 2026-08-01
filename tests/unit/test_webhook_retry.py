from __future__ import annotations

from datetime import datetime, timedelta, timezone

from inktime.app.services.notifications import DeviceNotificationService
from inktime.app.workers.runner import WorkerRunner


class _Response:
    def __init__(self, status_code: int, headers: dict | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


class _SequenceSession:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _enable(app) -> None:
    settings = app.extensions["inktime_settings_repository"]
    settings.update("notification.webhook_enabled", True, changed_by="test", source_ip="local")
    settings.update(
        "notification.webhook_url",
        "https://hooks.example.test/inktime",
        changed_by="test",
        source_ip="local",
    )


def test_webhook_retry_is_persisted_idempotent_and_survives_service_restart(app):
    _enable(app)
    original = app.extensions["inktime_notification_service"]
    original.session = _SequenceSession([_Response(500)])
    notification_id = original.create_test(created_by="test")
    now = datetime(2026, 7, 26, 12, tzinfo=timezone.utc)

    outcome = original.deliver_one(notification_id, now=now)
    assert outcome["status"] == "retrying"
    stored = original.list()[0]
    assert stored["webhook_attempts"] == 1
    assert datetime.fromisoformat(stored["webhook_next_attempt_at"]) > now

    restarted = DeviceNotificationService(
        app.extensions["inktime_database"],
        app.extensions["inktime_settings_repository"],
        app.extensions["inktime_secret_store"],
        session=_SequenceSession([_Response(204)]),
    )
    due = datetime.fromisoformat(stored["webhook_next_attempt_at"]) + timedelta(seconds=1)
    assert restarted.deliver_pending(now=due)["delivered"] == 1
    assert restarted.deliver_pending(now=due + timedelta(minutes=1))["delivered"] == 0
    assert restarted.list()[0]["webhook_status"] == "delivered"
    first_request = original.session.calls[0][1]
    retry_request = restarted.session.calls[0][1]
    event_id = stored["webhook_idempotency_key"]
    assert first_request["headers"]["Idempotency-Key"] == event_id
    assert first_request["headers"]["X-InkTime-Event-ID"] == event_id
    assert first_request["json"]["event_id"] == event_id
    assert retry_request["headers"]["Idempotency-Key"] == event_id
    assert retry_request["headers"]["X-InkTime-Event-ID"] == event_id
    assert retry_request["json"]["event_id"] == event_id


def test_webhook_retry_after_is_capped_and_general_4xx_is_terminal(app):
    _enable(app)
    service = DeviceNotificationService(
        app.extensions["inktime_database"],
        app.extensions["inktime_settings_repository"],
        app.extensions["inktime_secret_store"],
        session=_SequenceSession([_Response(429, {"Retry-After": "99999"}), _Response(400)]),
        retry_max_seconds=300,
    )
    now = datetime(2026, 7, 26, 12, tzinfo=timezone.utc)
    first = service.create_test(created_by="one")
    second = service.create_test(created_by="two")

    assert service.deliver_one(first, now=now)["status"] == "retrying"
    first_row = next(row for row in service.list() if row["id"] == first)
    assert datetime.fromisoformat(first_row["webhook_next_attempt_at"]) == now + timedelta(seconds=300)
    assert service.deliver_one(second, now=now)["status"] == "failed"


def test_webhook_scheduler_claim_is_bounded_and_does_not_make_http_calls(app):
    _enable(app)
    service = app.extensions["inktime_notification_service"]
    fake = _SequenceSession([])
    service.session = fake
    for actor in ("one", "two", "three"):
        service.create_test(created_by=actor)

    result = service.enqueue_pending(
        app.extensions["inktime_job_repository"],
        app.extensions["inktime_job_service"],
        limit=2,
    )

    assert result == {"claimed": 2, "enqueued": 2}
    assert fake.calls == []
    assert app.extensions["inktime_job_repository"].active_count("webhook") == 2


def test_webhook_job_uses_existing_worker_queue_and_separate_timeouts(app):
    _enable(app)
    service = app.extensions["inktime_notification_service"]
    fake = _SequenceSession([_Response(204)])
    service.session = fake
    notification_id = service.create_test(created_by="worker")
    service.enqueue_pending(
        app.extensions["inktime_job_repository"],
        app.extensions["inktime_job_service"],
        limit=1,
    )

    WorkerRunner(app).run_once()

    row = next(item for item in service.list() if item["id"] == notification_id)
    assert row["webhook_status"] == "delivered"
    request = fake.calls[0][1]
    assert isinstance(request["timeout"], tuple) and len(request["timeout"]) == 2
    assert request["headers"]["Idempotency-Key"] == row["webhook_idempotency_key"]
    assert request["headers"]["X-InkTime-Event-ID"] == row["webhook_idempotency_key"]
    assert request["json"]["event_id"] == row["webhook_idempotency_key"]
