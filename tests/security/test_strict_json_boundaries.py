from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from tests.conftest import create_admin, csrf, login


INVALID_INTEGERS = [True, False, "1", "10", 1.0, 1.5, None, [], {}]
INVALID_FLOATS = [True, False, "1.0", float("nan"), float("inf"), float("-inf"), [], {}]


@pytest.fixture
def administrator(client, app):
    create_admin(app)
    login(client)
    return {"X-CSRF-Token": csrf(client)}


@pytest.mark.parametrize("value", INVALID_INTEGERS)
@pytest.mark.parametrize("field", ["limit"])
def test_jobs_reject_string_boolean_and_fractional_limits(client, administrator, field, value):
    response = client.post(
        "/api/v1/jobs",
        json={field: value},
        headers=administrator,
    )
    assert response.status_code == 400


@pytest.mark.parametrize("value", INVALID_FLOATS)
def test_job_budget_rejects_ambiguous_or_nonfinite_numbers(client, administrator, value):
    response = client.post(
        "/api/v1/jobs",
        json={"budget_limit": value},
        headers=administrator,
    )
    assert response.status_code == 400


@pytest.mark.parametrize("value", INVALID_INTEGERS)
def test_job_estimate_rejects_invalid_photo_count(client, administrator, value):
    response = client.post(
        "/api/v1/jobs/estimate",
        json={"photo_count": value},
        headers=administrator,
    )
    assert response.status_code == 400


@pytest.mark.parametrize("field", ["timeout_seconds", "retry_count", "retry_interval_seconds"])
@pytest.mark.parametrize("value", [True, "60", 60.5])
def test_schedule_bounded_fields_require_json_integer(client, administrator, field, value):
    response = client.patch(
        "/api/v1/schedules/incremental_scan",
        json={field: value},
        headers=administrator,
    )
    assert response.status_code == 400


def test_schedule_weekdays_reject_boolean(client, administrator):
    response = client.patch(
        "/api/v1/schedules/incremental_scan",
        json={"weekdays": [True]},
        headers=administrator,
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "config",
    [
        {"batch_size": "500"},
        {"concurrency": True},
        {"missing_safe_percent": 1.5},
        {"build_thumbnails": 1},
    ],
)
def test_schedule_nested_config_rejects_ambiguous_scalars(client, administrator, config):
    response = client.patch(
        "/api/v1/schedules/incremental_scan",
        json={"config": config},
        headers=administrator,
    )
    assert response.status_code == 400


@pytest.mark.parametrize("path", ["estimate", "cleanup"])
@pytest.mark.parametrize("field", ["max_bytes", "retention_days"])
@pytest.mark.parametrize("value", [True, "30", 30.5])
def test_cache_cleanup_and_estimate_reject_ambiguous_numbers(client, administrator, path, field, value):
    response = client.post(
        f"/api/v1/maintenance/cache/{path}",
        json={field: value},
        headers=administrator,
    )
    assert response.status_code == 400


@pytest.mark.parametrize("value", INVALID_INTEGERS)
def test_feedback_days_requires_json_integer(client, administrator, value):
    response = client.post(
        "/api/feedback",
        json={"feedback_type": "SKIP_TEMPORARILY", "photo_id": "photo", "days": value},
        headers=administrator,
    )
    assert response.status_code == 400


@pytest.mark.parametrize("value", INVALID_FLOATS)
def test_feedback_value_requires_finite_number(client, administrator, value):
    response = client.post(
        "/api/feedback",
        json={"feedback_type": "LIKE", "photo_id": "photo", "value": value},
        headers=administrator,
    )
    assert response.status_code == 400


@pytest.mark.parametrize("field", ["depth", "priority"])
@pytest.mark.parametrize("value", [True, "3", 3.5])
def test_queue_depth_and_priority_require_json_integer(client, app, administrator, field, value):
    device_id, _token = app.extensions["inktime_device_repository"].create("queue-strict")
    response = client.post(
        f"/api/devices/{device_id}/queue/generate",
        json={field: value},
        headers=administrator,
    )
    assert response.status_code == 400


@pytest.mark.parametrize("value", INVALID_INTEGERS)
def test_queue_ack_version_requires_json_integer(client, app, value):
    _device_id, token = app.extensions["inktime_device_repository"].create("ack-strict")
    response = client.post(
        "/api/device/v1/queue/ack",
        json={
            "queue_item_id": "item",
            "queue_version": value,
            "event": "MANIFEST_RECEIVED",
            "idempotency_key": "key",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400


def test_queue_ack_rejects_oversized_idempotency_key(client, app):
    _device_id, token = app.extensions["inktime_device_repository"].create("ack-key-limit")
    response = client.post(
        "/api/device/v1/queue/ack",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "queue_item_id": "item",
            "queue_version": 0,
            "event": "MANIFEST_RECEIVED",
            "idempotency_key": "x" * 129,
        },
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "field",
    [
        "priority",
        "max_concurrency",
        "timeout_seconds",
        "cooldown_seconds",
        "rate_limit_rpm",
        "token_limit_tpm",
    ],
)
@pytest.mark.parametrize("value", [True, "10", 10.5])
def test_provider_numeric_settings_require_json_numbers(client, administrator, field, value):
    response = client.post(
        "/api/v1/providers",
        json={"name": "strict", "base_url": "https://example.com/v1", field: value},
        headers=administrator,
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "payload",
    [
        {"start_year": "2020"},
        {"end_year": True},
        {"exclude_recent_days": 1.5},
        {"mode": "top_n", "top_n": "10", "month_day": "01-01"},
    ],
)
def test_history_rendering_numeric_filters_require_json_integers(client, administrator, payload):
    path = "/api/v1/rendering/history/reroll" if "top_n" in payload else "/api/v1/rendering/history/select"
    response = client.post(path, json=payload, headers=administrator)
    assert response.status_code == 400


@pytest.mark.parametrize("payload", [[], None, "value", 1, True])
def test_top_level_non_object_json_is_rejected(client, administrator, payload):
    response = client.post(
        "/api/v1/jobs/estimate",
        data=json.dumps(payload),
        content_type="application/json",
        headers=administrator,
    )
    assert response.status_code == 400


def test_wrong_content_type_is_rejected(client, administrator):
    response = client.post(
        "/api/v1/jobs/estimate",
        data='{"photo_count": 1}',
        content_type="text/plain",
        headers=administrator,
    )
    assert response.status_code == 415


def test_oversized_json_body_is_rejected(client, administrator):
    response = client.post(
        "/api/v1/jobs/estimate",
        data='{"padding":"' + ("x" * (257 * 1024)) + '"}',
        content_type="application/json",
        headers=administrator,
    )
    assert response.status_code == 413


def test_api_boundaries_do_not_bypass_shared_json_loader_or_coerce_scalars():
    root = Path(__file__).resolve().parents[2] / "inktime" / "app" / "api"
    violations: list[str] = []
    for path in sorted(root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "request.get_json" in source:
            violations.append(f"{path.name}: direct request.get_json")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in {"int", "float", "bool"} or not node.args:
                continue
            argument = ast.get_source_segment(source, node.args[0]) or ""
            if "payload" in argument:
                violations.append(f"{path.name}:{node.lineno}: {node.func.id}({argument})")
    assert violations == []
