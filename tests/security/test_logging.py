from __future__ import annotations

import json
import logging

from inktime.app.core.logging import HumanFormatter, JsonFormatter
from inktime.app.core.security import register_secret


def test_structured_log_redacts_secrets():
    record = logging.LogRecord("provider", logging.ERROR, "", 0, "連線失敗", (), None)
    record.component = "provider"
    record.event = "request_failed"
    record.error_code = "VLM-001"
    record.details = {"api_key": "sk-secret", "nested": {"device_token": "itd-secret"}, "safe": "ok"}
    payload = json.loads(JsonFormatter().format(record))
    assert payload["details"]["api_key"] == "[已遮蔽]"
    assert payload["details"]["nested"]["device_token"] == "[已遮蔽]"
    assert "sk-secret" not in json.dumps(payload)
    assert set(payload) == {
        "timestamp",
        "level",
        "component",
        "event",
        "error_code",
        "message",
        "job_id",
        "photo_id",
        "provider",
        "model",
        "duration_ms",
        "retry_count",
        "details",
    }


def test_full_api_key_is_redacted_from_plain_exception_messages():
    api_key = "vendor-key-0123456789-super-secret"
    register_secret(api_key)
    record = logging.LogRecord(
        "provider",
        logging.ERROR,
        "",
        0,
        f"upstream rejected Authorization Bearer {api_key}",
        (),
        None,
    )

    human = HumanFormatter().format(record)
    structured = JsonFormatter().format(record)

    assert api_key not in human
    assert api_key not in structured
    assert "[已遮蔽]" in human
    assert "[已遮蔽]" in structured
