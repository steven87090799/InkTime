from __future__ import annotations

from datetime import datetime, timedelta, timezone


class _Response:
    status_code = 204


class _WebhookSession:
    def __init__(self) -> None:
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response()


def _update_setting(app, key, value):
    app.extensions["inktime_settings_repository"].update(
        key, value, changed_by="test", source_ip="127.0.0.1"
    )


def test_offline_notification_is_deduplicated_and_recovery_is_recorded(app):
    repository = app.extensions["inktime_device_repository"]
    device_id, _ = repository.create("客廳電子紙")
    now = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
    old = (now - timedelta(hours=40)).isoformat()
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE devices SET created_at=?,updated_at=? WHERE id=?", (old, old, device_id)
        )

    service = app.extensions["inktime_notification_service"]
    assert service.scan(now=now)["offline"] == 1
    assert service.scan(now=now + timedelta(minutes=10))["offline"] == 0
    assert len(service.list()) == 1

    recovered_at = (now + timedelta(hours=1)).isoformat()
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE devices SET last_seen_at=?,updated_at=? WHERE id=?",
            (recovered_at, recovered_at, device_id),
        )
    assert service.scan(now=now + timedelta(hours=2))["recovery"] == 1
    device = repository.list()[0]
    assert device["offline_alert_active"] == 0
    assert [row["kind"] for row in service.list()] == ["recovery", "offline"]


def test_webhook_token_is_not_in_payload_and_delivery_is_persisted(app):
    _update_setting(app, "notification.webhook_enabled", True)
    _update_setting(app, "notification.webhook_url", "https://hooks.example.test/inktime")
    app.extensions["inktime_secret_store"].set(
        "notification.webhook_token", "top-secret", "test"
    )
    service = app.extensions["inktime_notification_service"]
    fake = _WebhookSession()
    service.session = fake
    service.create_test(created_by="test")

    result = service.deliver_pending()

    assert result["delivered"] == 1
    assert fake.calls[0][1]["headers"]["Authorization"] == "Bearer top-secret"
    assert "top-secret" not in str(fake.calls[0][1]["json"])
    assert service.list()[0]["webhook_status"] == "delivered"
