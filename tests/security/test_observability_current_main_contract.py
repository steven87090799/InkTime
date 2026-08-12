from __future__ import annotations

import ast
from pathlib import Path

from inktime.app.core.logging import get_log_context


ROOT = Path(__file__).resolve().parents[2]


def _events(relative_path: str) -> set[str]:
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    keyword_events = {
        str(value.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "event"
        for value in ast.walk(keyword.value)
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    }
    activity_events = {
        str(value.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_activity"
        and len(node.args) >= 2
        for value in ast.walk(node.args[1])
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    }
    return keyword_events | activity_events


def test_request_id_validation_response_header_and_context_cleanup(client):
    baseline = get_log_context()
    accepted = client.get("/health/live", headers={"X-Request-ID": "req-valid_123"})
    rejected = client.get("/health/live", headers={"X-Request-ID": "Bearer private token"})

    assert accepted.headers["X-Request-ID"] == "req-valid_123"
    assert rejected.headers["X-Request-ID"] != "Bearer private token"
    assert len(rejected.headers["X-Request-ID"]) == 32
    assert get_log_context() == baseline
    assert "request_id" not in get_log_context()


def test_current_process_boundary_and_batch_lifecycle_have_structured_events():
    boundary = _events("inktime/app/workers/process_boundary.py")
    batch = _events("inktime/app/services/batch_analysis.py")

    assert {
        "boundary_call_start",
        "boundary_call_success",
        "boundary_call_timeout",
        "boundary_call_error",
        "boundary_process_terminated",
    } <= boundary
    assert {
        "batch_prepare",
        "batch_submit_completed",
        "batch_poll",
        "batch_completed",
        "batch_failed",
        "batch_result_ingest",
        "batch_restart_recovery",
    } <= batch


def test_openai_batch_parameter_does_not_shadow_requests_module():
    source = (ROOT / "inktime/app/providers/openai_compatible.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    submit = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "submit_batch"
    )

    assert "requests" not in {argument.arg for argument in submit.args.args}
    assert "batch_requests" in {argument.arg for argument in submit.args.args}


def test_firmware_observability_is_bounded_and_does_not_print_sensitive_values():
    for directory in ("ink-display-7C-photo", "ink-display-133C-photo"):
        root = ROOT / "esp32" / directory
        header = (root / "firmware_observability.h").read_text(encoding="utf-8")
        sketch = next(root.glob("*.ino")).read_text(encoding="utf-8")

        assert "#define INKTIME_LOG_LEVEL 2" in header
        assert "INK_LOG_INFO" in sketch
        assert "DBG_PRINTLN(cfg.wifi_ssid)" not in sketch
        assert "DBG_PRINTLN(cfg.wifi_pass)" not in sketch
        assert "DBG_PRINTLN(cfg.backend_hostport)" not in sketch
        assert "DBG_PRINTLN(url)" not in sketch
