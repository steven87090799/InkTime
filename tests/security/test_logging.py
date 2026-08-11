from __future__ import annotations

import json
import logging
from uuid import uuid4

from inktime.app.core.logging import (
    STANDARD_FIELDS,
    HumanFormatter,
    JsonFormatter,
    bind_log_context,
    clear_log_context,
    get_log_context,
    log_event,
    should_log_rate_limited,
    should_log_sample,
)
from inktime.app.core.security import redact, redact_text, register_secret


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
    assert set(payload) == set(STANDARD_FIELDS)
    assert payload["schema_version"] == 1


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


def test_nested_redaction_covers_headers_credentials_and_url_queries():
    value = {
        "headers": {
            "Authorization": "Basic dXNlcjpwYXNzd29yZA==",
            "Cookie": "session=private-value",
        },
        "nested": [
            {"pairing_code": "123456", "job_id": "job-safe"},
            "https://example.test/callback?token=private&view=summary&signature=signed",
        ],
    }

    rendered = json.dumps(redact(value), ensure_ascii=False)

    assert "dXNlcjpwYXNzd29yZA" not in rendered
    assert "private-value" not in rendered
    assert "123456" not in rendered
    assert "token=private" not in rendered
    assert "signature=signed" not in rendered
    assert "job-safe" in rendered
    assert "view=summary" in rendered


def test_exception_metadata_is_redacted_and_bounded():
    secret = "sk-" + "s" * 40
    register_secret(secret)
    try:
        raise RuntimeError(f"Authorization Bearer {secret} " + "x" * 30_000)
    except RuntimeError as exc:
        record = logging.LogRecord(
            "provider", logging.ERROR, __file__, 1, "terminal provider failure", (), exc_info=None
        )
        record.event = "provider_protocol_error"
        record.exc_info = (type(exc), exc, exc.__traceback__)

    payload = json.loads(JsonFormatter().format(record))

    assert payload["event"] == "provider_protocol_error"
    assert payload["exception_type"] == "RuntimeError"
    assert secret not in json.dumps(payload)
    assert len(payload["exception_message"]) <= 2048
    assert len(payload["stack_trace"]) <= 12000


def test_context_propagates_explicit_override_and_cleanup():
    token = bind_log_context(request_id="req-123", job_id="job-context", trace_id="trace-123")
    logger = logging.getLogger(f"test-context-{uuid4()}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger.addHandler(Capture())
    try:
        log_event(logger, logging.DEBUG, "decision", event="branch_selected", job_id="job-explicit")
    finally:
        clear_log_context(token)

    assert len(records) == 1
    payload = json.loads(JsonFormatter().format(records[0]))
    assert payload["request_id"] == "req-123"
    assert payload["trace_id"] == "trace-123"
    assert payload["job_id"] == "job-explicit"
    assert payload["event"] == "branch_selected"
    assert get_log_context() == {}


def test_human_formatter_surfaces_only_high_value_context():
    record = logging.LogRecord("worker", logging.WARNING, "", 0, "Child timeout", (), None)
    record.event = "worker_child_timeout"
    record.error_code = "JOB-004"
    record.job_id = "job-1"
    record.job_item_id = "item-2"
    record.duration_ms = 120_000
    record.details = {"payload": "must-not-render"}

    rendered = HumanFormatter().format(record)

    assert "[worker_child_timeout]" in rendered
    assert "[JOB-004]" in rendered
    assert "job=job-1" in rendered
    assert "item=item-2" in rendered
    assert "duration_ms=120000" in rendered
    assert "must-not-render" not in rendered


def test_sampling_and_rate_limit_are_bounded_and_deterministic():
    assert [index for index in range(12) if should_log_sample(index, first=2, every=5)] == [
        0,
        1,
        5,
        10,
    ]
    key = f"test-rate-{uuid4()}"
    assert should_log_rate_limited(key, interval_seconds=10, now=100)
    assert not should_log_rate_limited(key, interval_seconds=10, now=105)
    assert should_log_rate_limited(key, interval_seconds=10, now=110)


def test_structured_details_are_bounded_before_serialization():
    record = logging.LogRecord("scanner", logging.DEBUG, "", 0, "bounded", (), None)
    record.event = "scan_progress"
    record.details = {f"item_{index}": index for index in range(1_000)}

    payload = json.loads(JsonFormatter().format(record))

    assert payload["details"]["_truncated"] is True
    assert len(payload["details"]) == 65


def test_redact_text_preserves_correlation_identifiers():
    value = "job_id=job-1 release_id=rel-2 device_id=dev-3 request_id=req-4"
    assert redact_text(value) == value
