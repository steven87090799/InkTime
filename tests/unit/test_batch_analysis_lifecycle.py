from __future__ import annotations

import json
from pathlib import Path

from PIL import Image
import pytest

from inktime.app.domain.photos import PhotoPreprocessor
from inktime.app.providers.base import VisionProvider
from inktime.app.providers.openai_compatible import ProviderHTTPError
from inktime.app.repositories.analysis_batches import AnalysisBatchRepository
from inktime.app.services.batch_analysis import BatchLifecycleError, stream_jsonl_shards
from inktime.app.workers.scanner import PhotoScanner
from tests.unit.test_analysis_schema import valid_result


class FakeBatchProvider(VisionProvider):
    name = "fake-batch"
    provider_id = "fake-batch"

    def __init__(self):
        self.custom_ids: list[str] = []
        self.deleted: list[str] = []
        self.downloads = 0
        self.recovered_batch_id = ""

    def build_analysis_request_body(self, **kwargs):
        return {
            "model": kwargs["model"],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "fake"}]}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "fake"}},
            "max_tokens": kwargs["max_tokens"],
        }

    def upload_batch_file(self, path: Path) -> str:
        self.custom_ids = [
            json.loads(line)["custom_id"] for line in path.read_text(encoding="utf-8").splitlines()
        ]
        return "file-input"

    def create_batch(self, input_file_id: str, **kwargs):
        return {
            "id": "remote-batch",
            "status": "validating",
            "input_file_id": input_file_id,
            "request_counts": {"total": len(self.custom_ids), "completed": 0, "failed": 0},
        }

    def retrieve_batch(self, batch_id: str):
        return {
            "id": batch_id,
            "endpoint": "/v1/chat/completions",
            "status": "completed",
            "input_file_id": "file-input",
            "metadata": {
                "inktime_batch_id": self.recovered_batch_id,
                "inktime_version": "batch-lifecycle-v1",
            },
            "output_file_id": "file-output",
            "request_counts": {"total": len(self.custom_ids), "completed": len(self.custom_ids), "failed": 0},
        }

    def download_file_content(self, file_id: str, destination: Path) -> Path:
        self.downloads += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        result = valid_result()
        lines = []
        for custom_id in reversed(self.custom_ids):
            body = {
                "choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
            lines.append(
                json.dumps(
                    {
                        "custom_id": custom_id,
                        "response": {"status_code": 200, "request_id": f"req-{custom_id}", "body": body},
                    },
                    ensure_ascii=False,
                )
            )
        destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return destination

    def delete_remote_file(self, file_id: str):
        self.deleted.append(file_id)
        return {"id": file_id, "deleted": True}

    def analyze(self, **kwargs):
        raise AssertionError("Fake Batch provider must not use synchronous analysis")

    def repair_json(self, **kwargs):
        raise AssertionError("Batch import must not repair with another model call")

    def submit_batch(self, requests, completion_window="24h"):
        raise AssertionError("production lifecycle must use streamed upload")

    def poll_batch(self, batch_id):
        return self.retrieve_batch(batch_id)

    def cancel_batch(self, batch_id):
        return {"id": batch_id, "status": "cancelled"}

    def estimate_cost(self, model, usage):
        return (usage.input_tokens + usage.output_tokens) / 100000.0

    def estimate_batch_cost(self, model, usage):
        return (usage.input_tokens + usage.output_tokens) / 200000.0

    def validate_config(self):
        return True, "ok"


class AmbiguousFakeBatchProvider(FakeBatchProvider):
    def __init__(self):
        super().__init__()
        self.create_calls = 0

    def create_batch(self, input_file_id: str, **kwargs):
        self.create_calls += 1
        self.recovered_batch_id = str((kwargs.get("metadata") or {}).get("inktime_batch_id") or "")
        raise ProviderHTTPError(
            "response lost after remote creation",
            "BATCH-SUBMISSION-UNKNOWN",
            ambiguous=True,
        )


def _prepare_photos(app, tmp_path, count=3):
    root = tmp_path / "photos"
    root.mkdir()
    for index in range(count):
        Image.new("RGB", (900, 600), (index % 256, (index * 3) % 256, (index * 7) % 256)).save(
            root / f"photo-{index}.jpg"
        )
    photos = app.extensions["inktime_photo_repository"]
    PhotoScanner(photos, PhotoPreprocessor(), app.extensions["inktime_thumbnail_cache"]).scan("照片", root)
    with app.extensions["inktime_database"].session() as connection:
        ids = [str(row[0]) for row in connection.execute("SELECT id FROM photos ORDER BY id").fetchall()]
        connection.executemany(
            "UPDATE photos SET eligible=1,exclusion_status='eligible',manual_override=0 WHERE id=?",
            [(photo_id,) for photo_id in ids],
        )
    return ids


def _wire_fake(app, fake):
    service = app.extensions["inktime_batch_analysis_service"]
    providers = app.extensions["inktime_provider_repository"]
    provider_service = app.extensions["inktime_provider_service"]
    providers.save(
        {
            "id": "fake-batch",
            "name": "Fake Batch",
            "base_url": "https://fake.invalid/v1",
            "enabled": True,
            "supports_batch": True,
            "supports_json_schema": True,
        },
        "tester",
    )
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "INSERT INTO model_pricing(provider_id,model,input_per_million,cached_input_per_million,output_per_million) VALUES (?,?,?,?,?)",
            ("fake-batch", "gpt-5.6-luna", 0.2, 0.02, 1.2),
        )
    providers.list = lambda: [
        {"id": "fake-batch", "name": "Fake Batch", "enabled": 1, "supports_batch": 1, "priority": 1}
    ]
    providers.pricing = lambda _provider_id: {
        "gpt-5.6-luna": {
            "input_per_million": 0.2,
            "cached_input_per_million": 0.02,
            "output_per_million": 1.2,
            "batch_multiplier": 0.5,
        }
    }
    provider_service.route_snapshot = lambda: [
        {"provider_id": "fake-batch", "display_name": "Fake Batch", "priority": 1, "config_revision": "v1"}
    ]
    provider_service.build_router = lambda *args, **kwargs: fake
    return service


def test_stream_jsonl_shards_use_actual_bytes_and_release_large_lines(tmp_path):
    items = [{"id": str(index)} for index in range(5)]
    shards = stream_jsonl_shards(
        tmp_path / "batch",
        items,
        lambda item: (item["id"] * 40).encode("utf-8") + b"\n",
        max_items=500,
        max_bytes=100,
    )
    assert len(shards) == 3
    assert [shard["items_count"] for shard in shards] == [2, 2, 1]
    assert all(Path(shard["path"]).stat().st_mode & 0o777 == 0o600 for shard in shards)


def test_stream_jsonl_shards_reject_single_item_over_limit(tmp_path):
    try:
        stream_jsonl_shards(
            tmp_path / "batch",
            [{"id": "one"}],
            lambda _item: b"x" * 101,
            max_bytes=100,
        )
    except BatchLifecycleError as exc:
        assert exc.code == "BATCH-INPUT-TOO-LARGE"
    else:
        raise AssertionError("single oversized JSONL line must fail closed")


def test_stream_jsonl_shards_split_at_500_requests(tmp_path):
    items = [{"id": str(index)} for index in range(501)]
    shards = stream_jsonl_shards(
        tmp_path / "batch",
        items,
        lambda item: (json.dumps({"id": item["id"]}) + "\n").encode("utf-8"),
        max_items=500,
        max_bytes=150 * 1024 * 1024,
    )
    assert [shard["items_count"] for shard in shards] == [500, 1]


def test_batch_privacy_candidate_excludes_never_upload(app, tmp_path):
    photo_ids = _prepare_photos(app, tmp_path, count=2)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET never_upload=1 WHERE id=?", (photo_ids[0],))
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    estimate = service.estimate(scope="all_eligible_missing_analysis")
    assert estimate["candidate_count"] == 1
    assert estimate["never_upload_excluded"] == 1


def test_fake_batch_imports_unordered_results_once(app, tmp_path):
    photo_ids = _prepare_photos(app, tmp_path)
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)

    submitted = service.submit(scope="sample", sample_count=100, created_by="tester")
    assert len(submitted["batch_ids"]) == 1
    batch_id = submitted["batch_ids"][0]
    assert fake.custom_ids
    first = service.poll_due(limit=10)
    assert first["polled"] == 1
    result = service.import_batch(batch_id)
    assert result["success"] == len(photo_ids)
    detail = service.get_detail(batch_id)
    assert detail["status"] == "completed"
    assert detail["imported_items"] == len(photo_ids)
    assert detail["missing_items"] == 0
    assert set(fake.deleted) == {"file-input", "file-output"}

    # Replaying the same Output File is safe: imported items, usage and the
    # analysis-batch unique index prevent duplicate durable results.
    service.import_batch(batch_id)
    with app.extensions["inktime_database"].session() as connection:
        analysis_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM photo_analysis WHERE analysis_source='analysis_batch'"
            ).fetchone()[0]
        )
        usage_count = int(
            connection.execute("SELECT COUNT(*) FROM api_usage WHERE processing_mode='batch'").fetchone()[0]
        )
    assert analysis_count == len(photo_ids)
    assert usage_count == len(photo_ids)


def test_ambiguous_batch_submission_is_persisted_and_recovery_only_binds_existing_remote(app, tmp_path):
    _prepare_photos(app, tmp_path, count=2)
    fake = AmbiguousFakeBatchProvider()
    service = _wire_fake(app, fake)

    submitted = service.submit(scope="sample", sample_count=100, created_by="tester")
    assert submitted["batch_ids"] == []
    batch_id = submitted["prepared_batch_ids"][0]
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    unknown = dict(repository.get(batch_id))
    assert unknown["status"] == "submission_unknown"
    assert unknown["remote_status"] == "submission_unknown"
    assert unknown["last_error_code"] == "submission_unknown"
    assert unknown["input_file_id"] == "file-input"
    assert all(item["error_code"] == "submission_unknown" for item in repository.items(batch_id))
    assert service.estimate(scope="all_eligible_missing_analysis")["candidate_count"] == 0
    with app.extensions["inktime_database"].session() as connection:
        assert (
            connection.execute(
                "SELECT status,completed_at FROM jobs WHERE id=?", (unknown["job_id"],)
            ).fetchone()["status"]
            == "running"
        )
    with app.extensions["inktime_database"].session() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM api_usage WHERE batch_id=?", (batch_id,)).fetchone()[0]
            == 0
        )

    recovered = service.recover_submission(batch_id, "batch-existing-123")
    assert recovered == {
        "batch_id": batch_id,
        "remote_batch_id": "batch-existing-123",
        "status": "import_pending",
    }
    assert fake.create_calls == 1
    bound = dict(repository.get(batch_id))
    assert bound["remote_batch_id"] == "batch-existing-123"
    assert bound["status"] == "import_pending"
    assert all(item["status"] == "submitted" for item in repository.items(batch_id))


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("endpoint", "/v1/responses", "BATCH-RECOVERY-OWNERSHIP-002"),
        ("input_file_id", "other-file", "BATCH-RECOVERY-OWNERSHIP-003"),
        (
            "metadata",
            {"inktime_batch_id": "other", "inktime_version": "batch-lifecycle-v1"},
            "BATCH-RECOVERY-OWNERSHIP-005",
        ),
        (
            "metadata",
            {"inktime_batch_id": "__BATCH_ID__", "inktime_version": "old"},
            "BATCH-RECOVERY-OWNERSHIP-006",
        ),
        ("request_counts", {"total": 999}, "BATCH-RECOVERY-OWNERSHIP-008"),
    ],
)
def test_recovery_ownership_mismatch_preserves_local_state_and_does_not_cleanup(
    app, tmp_path, field, value, code
):
    _prepare_photos(app, tmp_path, count=2)
    fake = AmbiguousFakeBatchProvider()
    service = _wire_fake(app, fake)
    submitted = service.submit(scope="sample", sample_count=100, created_by="tester")
    batch_id = submitted["prepared_batch_ids"][0]
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    before = dict(repository.get(batch_id))
    remote_id = "batch-existing-ownership"
    remote = {
        "id": remote_id,
        "endpoint": "/v1/chat/completions",
        "status": "validating",
        "input_file_id": "file-input",
        "metadata": {"inktime_batch_id": batch_id, "inktime_version": "batch-lifecycle-v1"},
        "request_counts": {"total": 2},
    }
    remote[field] = value
    if field == "metadata" and value.get("inktime_batch_id") == "__BATCH_ID__":
        remote[field] = {**value, "inktime_batch_id": batch_id}
    fake.retrieve_batch = lambda _batch_id: remote
    with pytest.raises(BatchLifecycleError) as raised:
        service.recover_submission(batch_id, remote_id)
    assert raised.value.code == code
    assert dict(repository.get(batch_id)) == before
    assert fake.deleted == []


def test_recovery_remote_404_preserves_local_state_and_does_not_cleanup(app, tmp_path):
    _prepare_photos(app, tmp_path, count=1)
    fake = AmbiguousFakeBatchProvider()
    service = _wire_fake(app, fake)
    batch_id = service.submit(scope="sample", sample_count=100, created_by="tester")["prepared_batch_ids"][0]
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    before = dict(repository.get(batch_id))

    def not_found(_batch_id):
        raise ProviderHTTPError("not found", "BATCH-REMOTE-404", http_status=404)

    fake.retrieve_batch = not_found
    with pytest.raises(ProviderHTTPError):
        service.recover_submission(batch_id, "batch-missing")
    assert dict(repository.get(batch_id)) == before
    assert fake.deleted == []


def test_cleanup_retry_only_retries_the_file_that_failed(app, tmp_path):
    _prepare_photos(app, tmp_path, count=2)
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    batch_id = service.submit(scope="sample", sample_count=100, created_by="tester")["batch_ids"][0]
    service.poll_due(limit=10)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    repository.update_batch(batch_id, error_file_id="file-error")
    failed_once = {"file-output"}
    original_delete = fake.delete_remote_file

    def delete(file_id):
        if file_id in failed_once:
            failed_once.remove(file_id)
            raise ProviderHTTPError("temporary cleanup failure", "BATCH-CLEANUP-RETRY", http_status=503)
        return original_delete(file_id)

    fake.delete_remote_file = delete
    service.import_batch(batch_id)
    detail = dict(repository.get(batch_id))
    assert detail["cleanup_status"] == "partial"
    assert detail["input_file_deleted"] == 1
    assert detail["output_file_deleted"] == 0
    assert detail["error_file_deleted"] == 1
    service.import_batch(batch_id, cleanup_only=True)
    assert fake.deleted.count("file-input") == 1
    assert fake.deleted.count("file-error") == 1
    assert fake.deleted.count("file-output") == 1
    assert dict(repository.get(batch_id))["cleanup_status"] == "completed"


def test_poll_plan_parse_failure_enters_cleanup_instead_of_staying_validating(app, tmp_path):
    _prepare_photos(app, tmp_path, count=1)
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    batch_id = service.submit(scope="sample", sample_count=100, created_by="tester")["batch_ids"][0]
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    batch = dict(repository.get(batch_id))
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE jobs SET analysis_spec_json=? WHERE id=?", ("{", batch["job_id"]))
    result = service.poll_due(limit=10)
    assert result["enqueued"] == 1
    current = dict(repository.get(batch_id))
    assert current["status"] == "cleanup_pending"
    assert current["last_error_code"] == "BATCH-POLL-PLAN-001"
    service.import_batch(batch_id, cleanup_only=True)
    assert dict(repository.get(batch_id))["cleanup_status"] == "completed"


def test_restart_reconciliation_moves_each_external_phase_to_a_safe_exit(app, tmp_path):
    photo_ids = _prepare_photos(app, tmp_path, count=4)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    for index, phase in enumerate(("uploading", "submitting", "validating", "preparing")):
        batch_id = service.submit(
            scope="manual_selection", photo_ids=[photo_ids[index]], created_by="tester"
        )["batch_ids"][0]
        repository.update_batch(
            batch_id,
            status=phase,
            remote_batch_id=None,
            phase_started_at="2000-01-01T00:00:00+00:00",
        )
        service.poll_due(limit=10)
        current = dict(repository.get(batch_id))
        if phase == "uploading":
            assert current["status"] == "upload_unknown"
        elif phase in {"submitting", "validating"}:
            assert current["status"] == "submission_unknown"
        else:
            assert current["status"] == "failed"


def test_cancel_without_remote_batch_cleans_input_and_abandon_releases_unknown(app, tmp_path):
    photo_ids = _prepare_photos(app, tmp_path, count=2)
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    batch_id = service.submit(scope="manual_selection", photo_ids=[photo_ids[0]], created_by="tester")[
        "batch_ids"
    ][0]
    repository.update_batch(batch_id, status="uploaded", remote_batch_id=None)
    cancelled = service.cancel(batch_id)
    assert cancelled["status"] == "cleanup_pending"
    service.import_batch(batch_id, cleanup_only=True)
    assert dict(repository.get(batch_id))["cleanup_status"] == "completed"

    ambiguous = AmbiguousFakeBatchProvider()
    service.provider_service.build_router = lambda *args, **kwargs: ambiguous
    unknown_id = service.submit(scope="manual_selection", photo_ids=[photo_ids[1]], created_by="tester")[
        "prepared_batch_ids"
    ][0]
    repository.update_batch(unknown_id, input_file_id=None, cleanup_status="not_required")
    result = service.abandon(unknown_id, confirmed_no_remote=True)
    assert result["status"] == "failed"


def test_control_plane_fake_batch_100_sample_end_to_end(app, tmp_path):
    _prepare_photos(app, tmp_path, count=100)
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    submitted = service.submit(scope="sample", sample_count=100, created_by="tester")
    assert submitted["candidate_count"] == 100
    batch_id = submitted["batch_ids"][0]
    service.poll_due(limit=10)
    imported = service.import_batch(batch_id)
    assert imported == {"batch_id": batch_id, "success": 100, "errors": 0, "missing": 0}
    detail = service.get_detail(batch_id)
    assert detail["status"] == "completed"
    assert detail["imported_items"] == 100
    assert detail["schema_success_rate"] == 100.0
    assert detail["actual_jsonl_bytes"] > 0
    assert detail["peak_rss_bytes"] > 0
