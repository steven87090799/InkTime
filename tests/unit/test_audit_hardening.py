"""Paid-evidence and contention regressions; all providers are local stubs."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from types import SimpleNamespace

import pytest

from inktime.app.providers.base import provider_process_timeout
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider, ProviderHTTPError, _request_json_size
from inktime.app.repositories.analysis_history import display_analysis_order_sql
from inktime.app.repositories.billable_operations import BillableOperationRepository
from inktime.app.repositories.usage import UsageRepository
from tests.conftest import create_admin, csrf, login


@pytest.mark.parametrize("body", [
    {}, {"中文": "引號\"與換行\n", "n": [1, True, None, -0.5]},
    {"messages": [{"url": "data:image/jpeg;base64," + "ABC+/=" * 1000}]},
    {"url": "data:image/jpeg;base64,invalid\"\n"},
])
def test_request_metrics_match_wire_json_without_copying_large_image(body):
    assert _request_json_size(body) == len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())


def test_long_retry_after_defers_safe_read_instead_of_retrying_early(monkeypatch):
    calls = []

    def get(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(status_code=429, headers={"Retry-After": "120"})

    provider = OpenAICompatibleProvider(name="test", base_url="https://example.com/v1", api_key="", session=SimpleNamespace(get=get))
    monkeypatch.setattr("inktime.app.providers.openai_compatible.time.sleep", lambda *_: pytest.fail("must defer, not sleep"))
    with pytest.raises(ProviderHTTPError) as caught:
        provider._send("GET", "/models")
    assert caught.value.retry_after == 120
    assert caught.value.http_status == 429
    assert len(calls) == 1


def test_retry_after_http_date_and_nonfinite_values():
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120), usegmt=True)
    delay = OpenAICompatibleProvider._retry_after(SimpleNamespace(headers={"Retry-After": future}))
    assert 118 <= delay <= 120
    for value in ("nan", "inf", "invalid", "-inf"):
        assert OpenAICompatibleProvider._retry_after(SimpleNamespace(headers={"Retry-After": value})) is None
    assert provider_process_timeout(SimpleNamespace(timeout=120)) == 135


def test_usage_increment_deduplicates_and_replaces_legacy_job_cost(app):
    database = app.extensions["inktime_database"]
    jobs = app.extensions["inktime_job_repository"]
    job = jobs.create_maintenance(kind="cleanup", name="cost", settings={}, created_by=None)
    with database.transaction() as connection:
        connection.execute("UPDATE jobs SET spent=99 WHERE id=?", (job,))
    usage = UsageRepository(database)
    values = dict(provider="test", model="model", job_id=job, photo_id=None, request_type="single",
                  input_tokens=1, output_tokens=1, cached_tokens=0, estimated_cost=0.02, actual_cost=None,
                  started_at=datetime.now(timezone.utc).isoformat(), latency_ms=1, status="completed", cost_source="estimated")
    operation, _ = BillableOperationRepository(database).begin("synthetic", "usage-test")
    usage.record(**values, operation_id=operation)
    usage.record(**values, operation_id=operation)
    usage.record(**{**values, "cost_source": "unknown", "estimated_cost": None})
    usage.record(**values)
    item = jobs.list_items(job)[0]
    assert jobs.complete_batch_item(job, item["id"], {}, actual_cost=123)
    assert not jobs.complete_batch_item(job, item["id"], {}, actual_cost=123)
    assert jobs.get(job)["spent"] == pytest.approx(0.04)


def test_display_order_uses_matching_expression_index(app):
    database = app.extensions["inktime_database"]
    with database.session() as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT a.id FROM photo_analysis a WHERE a.photo_id=? ORDER BY "  # noqa: S608 -- fixed repository ordering
            + display_analysis_order_sql() + " LIMIT 1", ("synthetic",),
        ).fetchall()
    details = " ".join(str(row[3]) for row in plan)
    assert "idx_analysis_display_order" in details
    assert "TEMP B-TREE" not in details


def test_scan_rejects_escape_and_symlink_outside_photo_root(app, client, tmp_path):
    create_admin(app)
    login(client)
    root = app.extensions["inktime_runtime_config"].photo_dir
    root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "outside-link"
    link.symlink_to(outside, target_is_directory=True)
    for path in (outside, link, root / ".."):
        response = client.post("/api/v1/maintenance/scan", json={"root_path": str(path)}, headers={"X-CSRF-Token": csrf(client)})
        assert response.status_code == 400


def test_manual_photo_selection_never_silently_truncates(app, client):
    create_admin(app)
    login(client)
    for endpoint in ("/api/v1/photos/exclusions/batch", "/api/v1/photos/exclusions/ai"):
        response = client.post(endpoint, json={"photo_ids": [str(index) for index in range(501)], "action": "reanalyze"}, headers={"X-CSRF-Token": csrf(client)})
        assert response.status_code == 400
        assert "500" in response.get_data(as_text=True)


def test_photo_history_is_bounded_and_older_receipts_remain_accessible(app, client):
    from tests.integration.test_jobs import add_photos

    create_admin(app)
    login(client)
    photo = add_photos(app, 1)[0]
    usage = UsageRepository(app.extensions["inktime_database"])
    for index in range(102):
        usage.record(provider="test", model="model", job_id=None, photo_id=photo,
                     request_type=f"page-marker-{index:03}", input_tokens=1, output_tokens=1, cached_tokens=0,
                     estimated_cost=0, actual_cost=0, started_at="2026-10-28T00:00:00+00:00", latency_ms=0,
                     status="completed", cost_source="provider_reported")
    first = client.get(f"/photos/{photo}").get_data(as_text=True)
    second = client.get(f"/photos/{photo}?history_page=2").get_data(as_text=True)
    assert "page-marker-101" in first and "page-marker-000" not in first
    assert "page-marker-000" in second and "page-marker-101" not in second
    assert "history_page=2" in first


def test_backup_retention_continues_after_one_unremovable_file(app, tmp_path, monkeypatch, caplog):
    from pathlib import Path

    service = app.extensions["inktime_backup_service"]
    keep, blocked, expired = [tmp_path / name for name in ("keep.zip", "blocked.zip", "expired.zip")]
    for path in (keep, blocked, expired):
        path.write_bytes(b"synthetic")
    monkeypatch.setattr(service, "list", lambda: [keep, blocked, expired])
    unlink = Path.unlink

    def remove(path, *, missing_ok=False):
        if path == blocked:
            raise PermissionError("synthetic NAS file lock")
        return unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", remove)
    assert service.enforce_retention(1) == 1
    assert keep.exists() and blocked.exists() and not expired.exists()
    assert "Backup retention could not remove" in caplog.text
