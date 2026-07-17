from __future__ import annotations

import json
import logging

from inktime.app.core.logging import JsonFormatter


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
    assert set(payload) == {"timestamp","level","component","event","error_code","message","job_id","photo_id","provider","model","duration_ms","retry_count","details"}
