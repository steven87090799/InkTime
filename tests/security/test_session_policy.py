from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

from flask import Flask, Request
from itsdangerous import TimestampSigner

from inktime.app.repositories.settings import SettingsRepository
from inktime.app.session_policy import SettingsSessionInterface


def test_session_expiry_reads_shared_policy_even_with_stale_process_configuration(app, monkeypatch):
    second = Flask("second-session-process")
    second.secret_key = app.secret_key
    second.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=5)
    second.extensions["inktime_settings_repository"] = SettingsRepository(app.extensions["inktime_database"])
    second.session_interface = SettingsSessionInterface()
    second.extensions["inktime_settings_repository"].get("security.session_minutes")
    interface = app.session_interface
    serializer = interface.get_signing_serializer(app)
    timestamp = int(time.time())
    with monkeypatch.context() as clock:
        clock.setattr(TimestampSigner, "get_timestamp", lambda self: timestamp - 6 * 60)
        cookie = serializer.dumps({"fixture": "session", "_permanent": True})
    request = Request.from_values(headers={"Cookie": "session=" + cookie})
    assert interface.open_session(app, request)["fixture"] == "session"
    assert second.session_interface.open_session(second, request)["fixture"] == "session"
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute("UPDATE settings SET value_json='5' WHERE key='security.session_minutes'")
    assert not interface.open_session(app, request)
    assert not second.session_interface.open_session(second, request)
    session = interface.session_class({"_permanent": True})
    expiry = interface.get_expiration_time(app, session)
    assert abs((expiry - datetime.now(timezone.utc) - timedelta(minutes=5)).total_seconds()) < 2
