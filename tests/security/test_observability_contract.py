from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _events(relative_path: str) -> dict[str, tuple[str, set[str]]]:
    path = ROOT / relative_path
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    result: dict[str, tuple[str, set[str]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.id if isinstance(function, ast.Name) else ""
        if name not in {"log_event", "_log_debug", "_log_failure"}:
            continue
        keywords = {item.arg: item.value for item in node.keywords if item.arg}
        event_node = keywords.get("event")
        if not isinstance(event_node, ast.Constant) or not isinstance(event_node.value, str):
            continue
        level = "DEBUG" if name == "_log_debug" else ""
        if name == "log_event" and len(node.args) >= 2:
            level = ast.get_source_segment(source, node.args[1]) or ""
        elif name == "_log_failure" and node.args:
            level = ast.get_source_segment(source, node.args[0]) or ""
        result[event_node.value] = (level, set(keywords))
    return result


def test_runtime_boundary_event_levels_and_identity_fields():
    contracts = {
        "inktime/app/workers/job_worker.py": {
            "worker_shutdown_requested": ("logging.INFO", {"worker_id"}),
            "worker_lease_renew_failed": (
                "logging.ERROR",
                {"error_code", "failure_class", "retryable"},
            ),
        },
        "inktime/app/providers/openai_compatible.py": {
            "provider_timeout": (
                "logging.WARNING",
                {"provider", "model", "duration_ms", "ambiguous"},
            ),
            "provider_schema_error": (
                "logging.ERROR",
                {"provider", "provider_request_id", "failure_class"},
            ),
            "batch_submitted": (
                "logging.INFO",
                {"provider", "batch_id", "duration_ms"},
            ),
        },
        "inktime/app/db/connection.py": {
            "db_writer_lock_timeout": (
                "logging.ERROR",
                {"duration_ms", "failure_class", "retryable"},
            ),
            "db_integrity_failed": ("logging.CRITICAL", {"error_code", "operation"}),
        },
        "inktime/app/db/migrations.py": {
            "migration_completed": ("logging.INFO", {"duration_ms", "phase"}),
            "migration_unknown_schema": (
                "logging.CRITICAL",
                {"error_code", "phase"},
            ),
        },
        "inktime/app/services/release_coordinator.py": {
            "release_published": ("logging.INFO", {"release_id", "duration_ms"}),
            "release_compensation_failed": (
                "logging.CRITICAL",
                {"release_id", "error_code", "failure_class"},
            ),
        },
    }

    for relative_path, expected in contracts.items():
        observed = _events(relative_path)
        for event, (level, required_fields) in expected.items():
            assert event in observed, f"{relative_path} is missing {event}"
            actual_level, fields = observed[event]
            assert actual_level == level
            assert required_fields <= fields


def test_logging_calls_do_not_pass_raw_sensitive_objects():
    forbidden = (
        "details=payload",
        "details=body",
        "details=headers",
        "details=specification",
        "details=request.get_json",
        "LOGGER.debug(",
        "LOGGER.exception(",
    )
    for path in (ROOT / "inktime").rglob("*.py"):
        compact = "".join(path.read_text(encoding="utf-8").split())
        for needle in forbidden:
            assert "".join(needle.split()) not in compact, f"unsafe logging pattern in {path}"
