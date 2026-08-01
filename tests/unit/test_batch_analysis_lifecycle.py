from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading
import time

from PIL import Image
import pytest

from inktime.app.domain.photos import PhotoPreprocessor
from inktime.app.providers.base import VisionProvider
from inktime.app.providers.openai_compatible import ProviderHTTPError
from inktime.app.repositories.analysis_batches import AnalysisBatchRepository
import inktime.app.services.batch_analysis as batch_analysis_module
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
        self.batch_counter = 0

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
        self.batch_counter += 1
        return {
            "id": "remote-batch" if self.batch_counter == 1 else f"remote-batch-{self.batch_counter}",
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


class UploadUnknownFakeBatchProvider(FakeBatchProvider):
    def __init__(self):
        super().__init__()
        self.upload_calls = 0
        self.create_calls = 0
        self.remote_filename = ""
        self.input_bytes = 0
        self.file_id = "file-ambiguous"
        self.file_metadata: dict[str, object] = {}

    def upload_batch_file(self, path: Path, *, remote_filename: str | None = None) -> str:
        self.upload_calls += 1
        self.remote_filename = str(remote_filename or path.name)
        self.input_bytes = path.stat().st_size
        self.custom_ids = [
            json.loads(line)["custom_id"] for line in path.read_text(encoding="utf-8").splitlines()
        ]
        raise ProviderHTTPError(
            "file response lost after remote creation",
            "BATCH-UPLOAD-UNKNOWN",
            ambiguous=True,
        )

    def retrieve_file(self, file_id: str):
        return dict(
            self.file_metadata
            or {
                "id": file_id,
                "purpose": "batch",
                "filename": self.remote_filename,
                "bytes": self.input_bytes,
                "provider_id": "fake-batch",
            }
        )

    def create_batch(self, input_file_id: str, **kwargs):
        self.create_calls += 1
        return super().create_batch(input_file_id, **kwargs)


class CountingBatchProvider(FakeBatchProvider):
    def __init__(self):
        super().__init__()
        self.upload_calls = 0
        self.create_calls = 0
        self._counter_lock = threading.Lock()

    def upload_batch_file(self, path: Path, *, remote_filename: str | None = None) -> str:
        with self._counter_lock:
            self.upload_calls += 1
        self.custom_ids = [
            json.loads(line)["custom_id"] for line in path.read_text(encoding="utf-8").splitlines()
        ]
        return "file-counted"

    def create_batch(self, input_file_id: str, **kwargs):
        with self._counter_lock:
            self.create_calls += 1
        return super().create_batch(input_file_id, **kwargs)


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
    provider_config = providers.get("fake-batch", include_secret=True)
    provider_service.route_snapshot = lambda: [
        {
            "provider_id": "fake-batch",
            "display_name": "Fake Batch",
            "priority": 1,
            "config_revision": provider_service.config_revision(provider_config),
        }
    ]
    provider_service.build_router = lambda *args, **kwargs: fake
    return service


def _insert_batch(app, batch_id: str, status: str, *, created_at: str = "2000-01-01T00:00:00+00:00"):
    now = created_at
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            """
            INSERT INTO analysis_batches(
                id,model,endpoint,analysis_fingerprint,status,remote_batch_id,
                created_at,updated_at,phase_started_at,last_polled_at
            ) VALUES (?,?,?,?,?,?,?,?,?,NULL)
            """,
            (
                batch_id,
                "gpt-5.6-luna",
                "/v1/chat/completions",
                f"fp-{batch_id}",
                status,
                "remote-live" if status == "in_progress" else None,
                now,
                now,
                now,
            ),
        )


def _future_lease() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


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


def test_scheduler_poll_excludes_unknown_holds_from_bounded_oldest_first_queue(app, monkeypatch):
    fake = CountingBatchProvider()
    service = _wire_fake(app, fake)
    repository = service.batches
    for index in range(25):
        _insert_batch(
            app,
            f"unknown-{index:02d}",
            "submission_unknown",
            created_at=f"2026-08-01T00:10:{index:02d}+00:00",
        )
    _insert_batch(app, "old-in-progress", "in_progress")
    _insert_batch(app, "old-cleanup", "cleanup_pending")
    fake.retrieve_batch = lambda batch_id: {
        "id": "remote-live",
        "status": "in_progress",
        "request_counts": {"total": 0, "completed": 0, "failed": 0},
    }
    monkeypatch.setattr(service, "_provider", lambda _provider_id, _plan: fake)
    enqueued: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        service,
        "_enqueue_import",
        lambda batch_id, cleanup_only=False: enqueued.append((batch_id, cleanup_only)) or "maintenance",
    )
    result = service.poll_due(limit=20)
    assert result == {"polled": 1, "enqueued": 1}
    assert ("old-cleanup", True) in enqueued
    assert fake.create_calls == 0
    assert fake.upload_calls == 0
    assert len(repository.list_operator_holds(limit=100)) == 25
    assert all(
        row["status"] not in {"upload_unknown", "submission_unknown"}
        for row in repository.list_pollable_due(limit=20)
    )

    # A later tick may revisit the live rows, but the unknown queue can never
    # displace them or trigger either external side effect.
    service.poll_due(limit=20)
    assert fake.create_calls == 0
    assert fake.upload_calls == 0


def test_concurrent_schedulers_claim_one_poll_side_effect(app, monkeypatch):
    _insert_batch(app, "concurrent-poll", "in_progress")
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    fake = CountingBatchProvider()
    service = _wire_fake(app, fake)
    poll_calls = {"count": 0}
    poll_lock = threading.Lock()

    def retrieve(_batch_id):
        with poll_lock:
            poll_calls["count"] += 1
        time.sleep(0.05)
        return {
            "id": "remote-live",
            "status": "in_progress",
            "request_counts": {"total": 0, "completed": 0, "failed": 0},
        }

    fake.retrieve_batch = retrieve
    monkeypatch.setattr(service, "_provider", lambda _provider_id, _plan: fake)
    original_claim = repository.claim_poll
    claim_barrier = threading.Barrier(2)

    def synchronized_claim(*args, **kwargs):
        result = original_claim(*args, **kwargs)
        claim_barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(repository, "claim_poll", synchronized_claim)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _value: service.poll_due(limit=1), range(2)))

    assert sum(result["polled"] for result in results) == 1
    assert poll_calls["count"] == 1
    assert dict(repository.get("concurrent-poll"))["status"] == "in_progress"


def test_poll_due_isolates_provider_creation_failure_per_iteration(app, monkeypatch):
    _insert_batch(app, "poll-provider-fails", "in_progress", created_at="2000-01-01T00:00:00+00:00")
    _insert_batch(app, "poll-provider-succeeds", "validating", created_at="2000-01-01T00:00:01+00:00")
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    repository.update_batch("poll-provider-succeeds", status="in_progress", remote_batch_id="remote-live-2")
    fake = CountingBatchProvider()
    service = _wire_fake(app, fake)
    fake.retrieve_batch = lambda batch_id: {
        "id": "remote-live" if batch_id == "remote-live" else "remote-live-2",
        "status": "in_progress",
        "request_counts": {"total": 0, "completed": 0, "failed": 0},
    }
    attempts = {"count": 0}

    def build_provider(_provider_id, _plan):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise BatchLifecycleError("injected poll provider failure", "BATCH-POLL-PROVIDER-001")
        return fake

    monkeypatch.setattr(service, "_provider", build_provider)
    result = service.poll_due(limit=2)
    assert result == {"polled": 1, "enqueued": 0}
    assert attempts["count"] == 2
    assert dict(repository.get("poll-provider-fails"))["last_error_code"] == "BATCH-POLL-PROVIDER-001"


def test_local_job_start_failure_releases_reservation_and_can_resubmit(app, tmp_path):
    photo_id = _prepare_photos(app, tmp_path, count=1)[0]
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    original_start = service.job_service.start

    def fail_start(_job_id):
        raise RuntimeError("injected job start failure")

    service.job_service.start = fail_start
    with pytest.raises(RuntimeError):
        service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")
    service.job_service.start = original_start
    failed_batches = repository.list(statuses={"failed"}, limit=10)
    assert failed_batches
    failed = failed_batches[0]
    assert failed["cleanup_status"] == "not_required"
    assert all(item["status"] == "failed" for item in repository.items(str(failed["id"])))
    with app.extensions["inktime_database"].session() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (failed["job_id"],)).fetchone()
        assert job["status"] == "failed"
        assert job["completed_at"] is not None
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM job_items WHERE job_id=? AND status IN ('pending','running','retrying')",
                (failed["job_id"],),
            ).fetchone()[0]
            == 0
        )
    resubmitted = service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")
    assert resubmitted["batch_ids"]


def test_provider_creation_failure_releases_prepared_batch_and_can_resubmit(app, tmp_path, monkeypatch):
    photo_id = _prepare_photos(app, tmp_path, count=1)[0]
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    original_provider = service._provider

    def fail_provider(*_args, **_kwargs):
        raise BatchLifecycleError("injected provider creation failure", "BATCH-PROVIDER-FAIL")

    monkeypatch.setattr(service, "_provider", fail_provider)
    with pytest.raises(BatchLifecycleError) as raised:
        service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")
    assert raised.value.code == "BATCH-PROVIDER-FAIL"
    failed = repository.list(statuses={"failed"}, limit=10)[0]
    assert failed["last_error_code"] == "BATCH-PROVIDER-FAIL"
    assert failed["cleanup_status"] == "not_required"
    assert all(item["status"] == "failed" for item in repository.items(str(failed["id"])))
    with app.extensions["inktime_database"].session() as connection:
        assert (
            connection.execute("SELECT status FROM jobs WHERE id=?", (failed["job_id"],)).fetchone()[0]
            == "failed"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM analysis_batch_items WHERE status IN ('pending','submitted','upload_unknown','submission_unknown')"
            ).fetchone()[0]
            == 0
        )
    monkeypatch.setattr(service, "_provider", original_provider)
    assert service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")["batch_ids"]


def test_jsonl_preparation_failure_fails_all_local_shards_and_can_resubmit(app, tmp_path, monkeypatch):
    photo_ids = _prepare_photos(app, tmp_path, count=2)
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])

    def fail_stream(*_args, **_kwargs):
        raise BatchLifecycleError("injected JSONL encoding failure", "BATCH-INPUT-ENCODE")

    monkeypatch.setattr(batch_analysis_module, "stream_jsonl_shards", fail_stream)
    with pytest.raises(BatchLifecycleError) as raised:
        service.submit(scope="manual_selection", photo_ids=photo_ids, created_by="tester")
    assert raised.value.code == "BATCH-INPUT-ENCODE"
    batches = repository.list(statuses={"failed"}, limit=20)
    assert batches
    for batch in batches:
        assert all(item["status"] == "failed" for item in repository.items(str(batch["id"])))
        assert batch["cleanup_status"] == "not_required"
    with app.extensions["inktime_database"].session() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM analysis_batch_items WHERE status IN ('pending','submitted','upload_unknown','submission_unknown')"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running','retrying') AND kind='analysis_batch'"
            ).fetchone()[0]
            == 0
        )
    monkeypatch.undo()
    resubmitted = service.submit(scope="manual_selection", photo_ids=photo_ids, created_by="tester")
    assert resubmitted["batch_ids"]


def test_child_shard_creation_failure_fails_parent_and_releases_all_items(app, tmp_path, monkeypatch):
    photo_ids = _prepare_photos(app, tmp_path, count=2)
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    monkeypatch.setattr(service, "_batch_limits", lambda: (1, 150 * 1024 * 1024))
    assert service._batch_limits()[0] == 1
    assert service.estimate(scope="manual_selection", photo_ids=photo_ids)["candidate_count"] == 2
    original_stream = batch_analysis_module.stream_jsonl_shards
    observed_shards: list[int] = []

    def observe_stream(*args, **kwargs):
        result = original_stream(*args, **kwargs)
        observed_shards.append(len(result))
        return result

    monkeypatch.setattr(batch_analysis_module, "stream_jsonl_shards", observe_stream)

    child_calls = []

    def fail_child(*_args, **_kwargs):
        child_calls.append(True)
        raise BatchLifecycleError("injected child creation failure", "BATCH-CHILD-001")

    monkeypatch.setattr(service.batches, "create_child_batch", fail_child)
    with pytest.raises(BatchLifecycleError) as raised:
        service.submit(scope="manual_selection", photo_ids=photo_ids, created_by="tester")
    assert observed_shards == [2]
    assert child_calls
    assert raised.value.code == "BATCH-CHILD-001"
    failed = repository.list(statuses={"failed"}, limit=10)
    assert failed
    job_id = str(failed[0]["job_id"])
    assert all(
        item["status"] == "failed"
        for batch in repository.list_for_job(job_id)
        for item in repository.items(str(batch["id"]))
    )
    assert not list(service.batch_root.glob("*/**/*.jsonl"))


@pytest.mark.parametrize(
    "field",
    ["filename", "purpose", "bytes", "provider_id"],
)
def test_upload_unknown_file_recovery_validates_metadata_without_second_upload(app, tmp_path, field):
    photo_id = _prepare_photos(app, tmp_path, count=1)[0]
    fake = UploadUnknownFakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    submitted = service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")
    batch_id = submitted["prepared_batch_ids"][0]
    before = dict(repository.get(batch_id))
    expected = {
        "id": fake.file_id,
        "purpose": "batch",
        "filename": fake.remote_filename,
        "bytes": fake.input_bytes,
        "provider_id": "fake-batch",
    }
    expected[field] = {
        "filename": "other.jsonl",
        "purpose": "fine-tune",
        "bytes": fake.input_bytes + 1,
        "provider_id": "other-provider",
    }[field]
    fake.file_metadata = expected
    with pytest.raises(BatchLifecycleError):
        service.recover_uploaded_file(batch_id, fake.file_id)
    assert dict(repository.get(batch_id)) == before
    assert fake.upload_calls == 1
    assert fake.deleted == []


def test_upload_unknown_recovery_then_submission_never_reuploads_file(app, tmp_path):
    photo_id = _prepare_photos(app, tmp_path, count=1)[0]
    fake = UploadUnknownFakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    batch_id = service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")[
        "prepared_batch_ids"
    ][0]
    with pytest.raises(BatchLifecycleError):
        service.abandon(batch_id, confirmed_no_remote=True)
    recovered = service.recover_uploaded_file(batch_id, fake.file_id)
    assert recovered["status"] == "uploaded"
    service.poll_due(limit=20)
    current = dict(repository.get(batch_id))
    assert current["status"] == "validating"
    assert current["input_file_id"] == fake.file_id
    assert fake.upload_calls == 1
    assert fake.create_calls == 1


def test_upload_unknown_abandon_requires_delete_evidence_and_releases_reservation(app, tmp_path):
    photo_id = _prepare_photos(app, tmp_path, count=1)[0]
    fake = UploadUnknownFakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    batch_id = service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")[
        "prepared_batch_ids"
    ][0]
    fake.delete_remote_file = lambda _file_id: (_ for _ in ()).throw(
        ProviderHTTPError("already deleted", "BATCH-FILE-NOT-FOUND", http_status=404)
    )
    abandoned = service.abandon(
        batch_id,
        confirmed_no_remote=True,
        remote_file_id=fake.file_id,
    )
    assert abandoned["remote_file_deleted"] is True
    current = dict(repository.get(batch_id))
    assert current["status"] == "failed"
    assert current["cleanup_status"] == "completed"
    assert current["input_file_id"] is None
    assert all(item["status"] == "failed" for item in repository.items(batch_id))

    def successful_upload(path: Path, *, remote_filename: str | None = None) -> str:
        fake.custom_ids = [
            json.loads(line)["custom_id"] for line in path.read_text(encoding="utf-8").splitlines()
        ]
        return "file-after-abandon"

    fake.upload_batch_file = successful_upload
    assert service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")["batch_ids"]


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
    assert unknown["reconciliation_error_code"] == "submission_unknown"
    assert unknown["last_error_code"] is None
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
    assert current["reconciliation_error_code"] == "BATCH-POLL-PLAN-001"
    service.import_batch(batch_id, cleanup_only=True)
    assert dict(repository.get(batch_id))["cleanup_status"] == "completed"


def test_restart_reconciliation_moves_each_external_phase_to_a_safe_exit(app, tmp_path):
    photo_ids = _prepare_photos(app, tmp_path, count=4)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    for index, phase in enumerate(("uploading", "submitting", "validating", "preparing")):
        submitted = service.submit(
            scope="manual_selection", photo_ids=[photo_ids[index]], created_by="tester"
        )
        assert submitted["batch_ids"], (index, phase, submitted)
        batch_id = submitted["batch_ids"][0]
        repository.update_batch(
            batch_id,
            status=phase,
            remote_batch_id=None,
            phase_started_at="2000-01-01T00:00:00+00:00",
        )
        service.poll_due(limit=10)
        current = dict(repository.get(batch_id))
        if phase == "uploading":
            # The input File ID is already durable, so restart recovery can
            # safely continue from uploaded without another POST /files.
            assert current["status"] == "uploaded"
        elif phase in {"submitting", "validating"}:
            assert current["status"] == "submission_unknown"
        else:
            # This fixture already has a persisted input File.  A stale
            # preparing marker must therefore enter cleanup, never pretend
            # that the remote side effect did not happen.
            assert current["status"] == "cleanup_pending"
            service.import_batch(batch_id, cleanup_only=True)
            assert dict(repository.get(batch_id))["status"] == "failed"
            with app.extensions["inktime_database"].session() as connection:
                assert (
                    connection.execute(
                        "SELECT status FROM job_items WHERE job_id=?", (current["job_id"],)
                    ).fetchone()[0]
                    == "failed"
                )


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


def test_local_cancel_and_abandon_reconcile_parent_job_items(app, tmp_path):
    photo_ids = _prepare_photos(app, tmp_path, count=2)
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])

    cancelled_id = service.submit(scope="manual_selection", photo_ids=[photo_ids[0]], created_by="tester")[
        "prepared_batch_ids"
    ][0]
    cancelled = dict(repository.get(cancelled_id))
    repository.update_batch(cancelled_id, status="uploaded", remote_batch_id=None)
    assert service.cancel(cancelled_id)["status"] == "cleanup_pending"
    service.import_batch(cancelled_id, cleanup_only=True)

    abandoned_id = service.submit(scope="manual_selection", photo_ids=[photo_ids[1]], created_by="tester")[
        "prepared_batch_ids"
    ][0]
    abandoned = dict(repository.get(abandoned_id))
    repository.update_batch(
        abandoned_id,
        status="submission_unknown",
        remote_status="submission_unknown",
        input_file_id=None,
        remote_batch_id=None,
        cleanup_status="not_required",
    )
    assert service.abandon(abandoned_id, confirmed_no_remote=True)["status"] == "failed"

    with app.extensions["inktime_database"].session() as connection:
        cancelled_item = connection.execute(
            "SELECT status FROM job_items WHERE job_id=?", (cancelled["job_id"],)
        ).fetchone()
        cancelled_job = connection.execute(
            "SELECT status FROM jobs WHERE id=?", (cancelled["job_id"],)
        ).fetchone()
        abandoned_item = connection.execute(
            "SELECT status FROM job_items WHERE job_id=?", (abandoned["job_id"],)
        ).fetchone()
        abandoned_job = connection.execute(
            "SELECT status FROM jobs WHERE id=?", (abandoned["job_id"],)
        ).fetchone()
    assert cancelled_item["status"] == "cancelled"
    assert cancelled_job["status"] == "cancelled"
    assert abandoned_item["status"] == "failed"
    assert abandoned_job["status"] in {"failed", "completed_with_errors"}


def test_cleanup_not_required_rejects_remote_file_id(app, tmp_path):
    photo_id = _prepare_photos(app, tmp_path, count=1)[0]
    service = _wire_fake(app, FakeBatchProvider())
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    batch_id = service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")[
        "prepared_batch_ids"
    ][0]
    with pytest.raises(sqlite3.IntegrityError):
        repository.update_batch(
            batch_id,
            status="failed",
            remote_status="failed",
            cleanup_status="not_required",
        )
    result = service.retry_cleanup(batch_id)
    assert result["status"] == "cleanup_pending"
    assert result["job_id"]
    assert dict(repository.get(batch_id))["cleanup_status"] == "pending"


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


def test_concurrent_submit_keeps_one_reservation_and_one_remote_batch(app, tmp_path):
    photo_ids = _prepare_photos(app, tmp_path, count=1)
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    reservation_barrier = threading.Barrier(2)
    original_create_with_items = service.batches.create_with_items

    def synchronized_create_with_items(*args, **kwargs):
        reservation_barrier.wait(timeout=10)
        return original_create_with_items(*args, **kwargs)

    service.batches.create_with_items = synchronized_create_with_items

    def submit_once():
        try:
            return service.submit(scope="manual_selection", photo_ids=photo_ids, created_by="tester")
        except BatchLifecycleError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(timeout=30) for future in [executor.submit(submit_once) for _ in range(2)]]

    assert sorted(result if isinstance(result, str) else "success" for result in results) == [
        "BATCH-RESERVATION-CONFLICT",
        "success",
    ]
    with app.extensions["inktime_database"].session() as connection:
        batch_count = int(connection.execute("SELECT COUNT(*) FROM analysis_batches").fetchone()[0])
        item_count = int(connection.execute("SELECT COUNT(*) FROM analysis_batch_items").fetchone()[0])
        valid_job_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind='analysis_batch' AND status NOT IN ('failed','cancelled')"
            ).fetchone()[0]
        )
    assert batch_count == 1
    assert item_count == 1
    assert valid_job_count == 1
    assert fake.custom_ids


def test_upload_and_submission_claims_are_compare_and_swap_single_winner(app):
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    _insert_batch(app, "claim-upload", "preparing")
    barrier = threading.Barrier(2)

    def upload_claim(index: int):
        barrier.wait(timeout=5)
        return repository.claim_upload("claim-upload", f"owner-{index}", f"upload-{index}", _future_lease())

    with ThreadPoolExecutor(max_workers=2) as executor:
        upload_results = list(executor.map(upload_claim, range(2)))
    assert sorted(upload_results) == [False, True]

    upload_owner = next(index for index, result in enumerate(upload_results) if result)
    repository.complete_upload(
        "claim-upload",
        f"upload-{upload_owner}",
        f"owner-{upload_owner}",
        "file-claim",
    )
    barrier = threading.Barrier(2)

    def submission_claim(index: int):
        barrier.wait(timeout=5)
        return repository.claim_submission(
            "claim-upload", f"submit-owner-{index}", f"submit-{index}", _future_lease()
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        submission_results = list(executor.map(submission_claim, range(2)))
    assert sorted(submission_results) == [False, True]


def test_concurrent_submitters_claim_external_upload_and_create_once(app, tmp_path):
    photo_id = _prepare_photos(app, tmp_path, count=1)[0]
    fake = CountingBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    submitted = service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")
    batch_id = submitted["batch_ids"][0]
    batch = dict(repository.get(batch_id))
    fake.upload_calls = 0
    fake.create_calls = 0
    repository.update_batch(
        batch_id,
        status="preparing",
        remote_status=None,
        input_file_id=None,
        remote_batch_id=None,
        upload_attempt_id=None,
        submission_attempt_id=None,
        last_error_code=None,
        last_error_message=None,
    )
    for item in repository.items(batch_id):
        repository.update_item(str(item["id"]), status="pending", error_code=None, error_message=None)
    plan = service._plan()[0]
    barrier = threading.Barrier(2)

    def submit_one():
        barrier.wait(timeout=5)
        try:
            service._submit_one(batch_id, plan)
            return "submitted"
        except BatchLifecycleError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _value: submit_one(), range(2)))
    assert outcomes.count("submitted") == 1
    assert fake.upload_calls == 1
    assert fake.create_calls == 1
    assert dict(repository.get(batch_id))["status"] == "validating"
    assert batch["input_file_id"] == "file-counted"


def test_late_remote_response_cannot_regress_terminal_or_cleanup_state(app):
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    _insert_batch(app, "monotonic", "validating")
    repository.update_batch("monotonic", remote_batch_id="remote-monotonic")
    completed = repository.set_status_from_remote(
        "monotonic",
        {"id": "remote-monotonic", "status": "completed", "output_file_id": "file-out"},
    )
    assert completed == "import_pending"
    after_completed = dict(repository.get("monotonic"))
    assert after_completed["completed_at"] is not None
    assert (
        repository.set_status_from_remote("monotonic", {"id": "remote-monotonic", "status": "in_progress"})
        == "ignored_stale"
    )
    assert dict(repository.get("monotonic"))["completed_at"] == after_completed["completed_at"]

    repository.update_batch("monotonic", status="cleanup_pending", cleanup_status="partial")
    before_cleanup = dict(repository.get("monotonic"))
    assert (
        repository.set_status_from_remote("monotonic", {"id": "remote-monotonic", "status": "completed"})
        == "ignored_stale"
    )
    current = dict(repository.get("monotonic"))
    assert current["status"] == before_cleanup["status"]
    assert current["output_file_id"] == before_cleanup["output_file_id"]


def test_recovery_binding_and_job_reopen_roll_back_as_one_transaction(app, tmp_path, monkeypatch):
    _prepare_photos(app, tmp_path, count=1)
    fake = AmbiguousFakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    batch_id = service.submit(scope="sample", sample_count=100, created_by="tester")["prepared_batch_ids"][0]
    before = dict(repository.get(batch_id))

    def fail_reopen(_job_id, *, connection=None):
        raise RuntimeError("injected reopen failure")

    monkeypatch.setattr(service.jobs, "reopen_batch_job", fail_reopen)
    with pytest.raises(RuntimeError):
        service.recover_submission(batch_id, "batch-atomic-recovery")
    assert dict(repository.get(batch_id)) == before
    assert all(item["status"] == "submission_unknown" for item in repository.items(batch_id))
    assert fake.deleted == []


def test_retry_cleanup_completed_is_idempotent_without_enqueue(app, tmp_path, monkeypatch):
    _prepare_photos(app, tmp_path, count=1)
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    batch_id = service.submit(scope="sample", sample_count=100, created_by="tester")["batch_ids"][0]
    service.poll_due(limit=20)
    service.import_batch(batch_id)
    calls: list[str] = []
    monkeypatch.setattr(service, "_enqueue_import", lambda *args, **kwargs: calls.append("enqueue"))
    assert service.retry_cleanup(batch_id) == {
        "status": "completed",
        "already_cleaned": True,
        "batch_id": batch_id,
    }
    assert service.retry_cleanup(batch_id)["already_cleaned"] is True
    assert calls == []


def test_cleanup_crash_after_remote_delete_reconciles_without_second_delete(app, tmp_path, monkeypatch):
    _prepare_photos(app, tmp_path, count=1)
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    batch_id = service.submit(scope="sample", sample_count=100, created_by="tester")["batch_ids"][0]
    service.poll_due(limit=20)
    repository.update_batch(batch_id, error_file_id="file-error")

    def already_gone(_file_id):
        raise ProviderHTTPError("file is gone", "BATCH-FILE-NOT-FOUND", http_status=404)

    fake.retrieve_file = already_gone
    original_complete = service.batches.complete_cleanup_file
    crashed = {"value": True}

    def crash_after_remote_delete(current_id, file_kind, owner):
        if crashed["value"]:
            crashed["value"] = False
            raise RuntimeError("injected process death after DELETE")
        return original_complete(current_id, file_kind, owner)

    monkeypatch.setattr(service.batches, "complete_cleanup_file", crash_after_remote_delete)
    with pytest.raises(RuntimeError):
        service.import_batch(batch_id)
    assert fake.deleted.count("file-input") == 1
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE analysis_batches SET side_effect_lease_until=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", batch_id),
        )
    monkeypatch.setattr(service.batches, "complete_cleanup_file", original_complete)
    service.import_batch(batch_id, cleanup_only=True)
    assert fake.deleted.count("file-input") == 1
    assert fake.deleted.count("file-output") == 1
    assert fake.deleted.count("file-error") == 1
    assert dict(repository.get(batch_id))["cleanup_status"] == "completed"


def test_concurrent_cleanup_workers_do_not_duplicate_delete_or_regress_success(app, tmp_path):
    photo_id = _prepare_photos(app, tmp_path, count=1)[0]
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    batch_id = service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")[
        "batch_ids"
    ][0]
    item_id = str(repository.items(batch_id)[0]["id"])
    repository.update_item(item_id, status="failed")
    repository.update_batch(
        batch_id,
        status="cleanup_pending",
        remote_status="failed",
        cleanup_final_action="complete",
        cleanup_status="partial",
        input_file_id="file-input",
        output_file_id="file-output",
        error_file_id="file-error",
        input_file_deleted=0,
        output_file_deleted=0,
        error_file_deleted=0,
    )
    original_delete = fake.delete_remote_file
    delete_lock = threading.Lock()

    def slow_delete(file_id: str):
        with delete_lock:
            result = original_delete(file_id)
            time.sleep(0.05)
            return result

    fake.delete_remote_file = slow_delete
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _value: service.import_batch(batch_id, cleanup_only=True), range(2))
        )

    assert all(result["cleanup_only"] is True for result in results)
    assert sorted(fake.deleted) == ["file-error", "file-input", "file-output"]
    final = dict(repository.get(batch_id))
    assert final["cleanup_status"] == "completed"
    assert final["status"] == "failed"


def _assert_terminal_batch_invariants(app, batch_id: str):
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    batch = dict(repository.get(batch_id))
    assert batch["status"] in {"completed", "completed_with_errors", "failed", "expired", "cancelled"}
    assert not {
        item["status"]
        for item in repository.items(batch_id)
        if item["status"] in {"pending", "submitted", "upload_unknown", "submission_unknown"}
    }
    assert batch["cleanup_status"] in {"completed", "not_required"}
    assert batch["cleanup_completed_at"] is not None
    if batch["input_file_id"]:
        assert batch["input_file_deleted"] == 1
    if batch["output_file_id"]:
        assert batch["output_file_deleted"] == 1
    if batch["error_file_id"]:
        assert batch["error_file_deleted"] == 1
    if batch["job_id"]:
        with app.extensions["inktime_database"].session() as connection:
            active_job_items = int(
                connection.execute(
                    "SELECT COUNT(*) FROM job_items WHERE job_id=? AND status IN ('pending','running','retrying')",
                    (batch["job_id"],),
                ).fetchone()[0]
            )
        assert active_job_items == 0


@pytest.mark.parametrize(
    ("initial_status", "remote_status", "item_status"),
    [
        ("completed", None, "imported"),
        ("completed_with_errors", None, "failed"),
        ("failed", None, "failed"),
        ("cancelled", None, "cancelled"),
        ("expired", None, "expired"),
    ],
)
def test_cleanup_retry_preserves_existing_terminal_semantic(
    app, tmp_path, initial_status, remote_status, item_status
):
    photo_id = _prepare_photos(app, tmp_path, count=1)[0]
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    batch_id = service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")[
        "batch_ids"
    ][0]
    item_id = str(repository.items(batch_id)[0]["id"])
    repository.update_item(item_id, status=item_status)
    repository.update_batch(
        batch_id,
        status=initial_status,
        remote_status=remote_status,
        completed_at="2026-08-01T00:00:00+00:00",
        input_file_id="file-input",
        output_file_id="file-output",
        error_file_id="file-error",
        cleanup_status="partial",
        cleanup_final_action="none",
        input_file_deleted=0,
        output_file_deleted=0,
        error_file_deleted=0,
    )

    cancelled = service.cancel(batch_id)
    assert cancelled["status"] == initial_status
    assert cancelled["cleanup_pending"] is True
    retried = service.retry_cleanup(batch_id)
    assert retried["status"] == initial_status
    assert retried["cleanup_pending"] is True
    service.import_batch(batch_id, cleanup_only=True)
    service.retry_cleanup(batch_id)
    service.import_batch(batch_id, cleanup_only=True)

    final = dict(repository.get(batch_id))
    assert final["status"] == initial_status
    assert final["cleanup_status"] == "completed"
    assert final["cleanup_final_action"] == ("cancel" if initial_status == "cancelled" else "complete")
    assert fake.deleted.count("file-input") == 1
    assert fake.deleted.count("file-output") == 1
    assert fake.deleted.count("file-error") == 1


@pytest.mark.parametrize(("intent", "terminal"), [("cancel", "cancelled"), ("abandon", "failed")])
def test_cleanup_final_action_survives_partial_delete_retry_and_atomically_finalizes(
    app, tmp_path, intent, terminal
):
    photo_id = _prepare_photos(app, tmp_path, count=1)[0]
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    batch_id = service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")[
        "batch_ids"
    ][0]
    if intent == "cancel":
        repository.update_batch(batch_id, status="uploaded", remote_batch_id=None)
    else:
        repository.update_batch(
            batch_id,
            status="submission_unknown",
            remote_status="submission_unknown",
            remote_batch_id=None,
        )
    repository.update_batch(batch_id, output_file_id="file-output", error_file_id="file-error")
    calls: dict[str, int] = {"file-input": 0, "file-output": 0, "file-error": 0}

    def delete_once_fails(file_id: str):
        calls[file_id] += 1
        if file_id == "file-output" and calls[file_id] == 1:
            raise ProviderHTTPError("transient cleanup failure", "BATCH-CLEANUP-500", http_status=500)
        fake.deleted.append(file_id)
        return {"id": file_id, "deleted": True}

    fake.delete_remote_file = delete_once_fails
    if intent == "cancel":
        assert service.cancel(batch_id)["status"] == "cleanup_pending"
    else:
        assert service.abandon(batch_id, confirmed_no_remote=True)["status"] == "cleanup_pending"
    service.import_batch(batch_id, cleanup_only=True)
    partial = dict(repository.get(batch_id))
    assert partial["cleanup_status"] == "partial"
    assert partial["cleanup_final_action"] == intent
    assert partial["cleanup_error_code"] == "BATCH-CLEANUP-500"
    service.retry_cleanup(batch_id)
    service.import_batch(batch_id, cleanup_only=True)
    final = dict(repository.get(batch_id))
    assert final["status"] == terminal
    assert final["cleanup_final_action"] == intent
    assert final["cleanup_status"] == "completed"
    assert calls == {"file-input": 1, "file-output": 2, "file-error": 1}
    _assert_terminal_batch_invariants(app, batch_id)


def test_upload_unknown_retry_cleanup_requires_operator_and_keeps_reservation(app, tmp_path):
    photo_id = _prepare_photos(app, tmp_path, count=1)[0]
    fake = UploadUnknownFakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    batch_id = service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")[
        "prepared_batch_ids"
    ][0]
    assert service.retry_cleanup(batch_id) == {"status": "operator_action_required", "batch_id": batch_id}
    assert service.retry_cleanup(batch_id) == {"status": "operator_action_required", "batch_id": batch_id}
    held = dict(repository.get(batch_id))
    assert held["status"] == "upload_unknown"
    assert held["cleanup_status"] == "pending"
    assert held["cleanup_completed_at"] is None
    assert held["cleanup_error_code"] == "BATCH-CLEANUP-UPLOAD-UNKNOWN"
    assert fake.upload_calls == 1
    assert fake.deleted == []
    assert all(item["status"] == "upload_unknown" for item in repository.items(batch_id))


def test_local_cancel_finalization_rolls_back_batch_items_and_parent_job_together(app, tmp_path, monkeypatch):
    photo_id = _prepare_photos(app, tmp_path, count=1)[0]
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = service.batches
    batch_id = service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")[
        "batch_ids"
    ][0]
    repository.update_batch(
        batch_id,
        status="uploaded",
        remote_status="uploaded",
        remote_batch_id=None,
        input_file_id=None,
    )
    before_batch = dict(repository.get(batch_id))
    before_items = [dict(item) for item in repository.items(batch_id)]
    original_totals = repository._item_totals_locked

    def fail_after_item_updates(*_args, **_kwargs):
        raise RuntimeError("injected finalization transaction failure")

    monkeypatch.setattr(repository, "_item_totals_locked", fail_after_item_updates)
    with pytest.raises(RuntimeError, match="injected finalization"):
        service.cancel(batch_id)
    monkeypatch.setattr(repository, "_item_totals_locked", original_totals)
    assert dict(repository.get(batch_id))["status"] == before_batch["status"]
    assert [dict(item)["status"] for item in repository.items(batch_id)] == [
        item["status"] for item in before_items
    ]
    with app.extensions["inktime_database"].session() as connection:
        job_status = connection.execute(
            "SELECT status FROM jobs WHERE id=?", (before_batch["job_id"],)
        ).fetchone()[0]
    assert job_status in {"running", "pending", "retrying"}


@pytest.mark.parametrize("kind", ["invalid_jsonl", "unexpected_custom_id"])
def test_reconciliation_error_survives_cleanup_and_finishes_with_errors(app, tmp_path, kind):
    photo_id = _prepare_photos(app, tmp_path, count=1)[0]
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    batch_id = service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")[
        "batch_ids"
    ][0]
    service.poll_due(limit=20)
    original_download = fake.download_file_content

    def download_with_reconciliation_error(file_id, destination):
        path = original_download(file_id, destination)
        if kind == "invalid_jsonl":
            path.open("a", encoding="utf-8").write("not-json\n")
        else:
            path.open("a", encoding="utf-8").write(
                json.dumps(
                    {
                        "custom_id": "ibt:ffffffff-ffff-ffff-ffff-ffffffffffff",
                        "response": {"status_code": 400, "body": {}},
                    }
                )
                + "\n"
            )
        return path

    fake.download_file_content = download_with_reconciliation_error
    result = service.import_batch(batch_id)
    assert result["success"] == 1
    final = dict(repository.get(batch_id))
    assert final["status"] == "completed_with_errors"
    assert final["reconciliation_error_code"] == kind
    assert final["cleanup_final_action"] == "complete"
    _assert_terminal_batch_invariants(app, batch_id)


@pytest.mark.parametrize(
    "identity_field",
    [
        "provider_base_url_fingerprint",
        "provider_project_id",
        "provider_account_fingerprint",
    ],
)
def test_malformed_frozen_plan_refuses_cleanup_when_provider_identity_changes(
    app, tmp_path, monkeypatch, identity_field
):
    photo_id = _prepare_photos(app, tmp_path, count=1)[0]
    fake = FakeBatchProvider()
    service = _wire_fake(app, fake)
    repository = AnalysisBatchRepository(app.extensions["inktime_database"])
    batch_id = service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")[
        "batch_ids"
    ][0]
    batch = dict(repository.get(batch_id))
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE jobs SET analysis_spec_json='{' WHERE id=?", (batch["job_id"],))
    original_identity = service.provider_service.identity_snapshot

    def changed_identity(provider_id):
        identity = dict(original_identity(provider_id))
        identity[identity_field] = "different-provider-context"
        return identity

    monkeypatch.setattr(service.provider_service, "identity_snapshot", changed_identity)
    result = service.import_batch(batch_id)
    assert result == {"batch_id": batch_id, "cleanup_only": True}
    held = dict(repository.get(batch_id))
    assert held["status"] == "cleanup_pending"
    assert held["cleanup_error_code"] == "BATCH-CLEANUP-PROVIDER-MISMATCH"
    assert held["input_file_deleted"] == 0
    assert fake.deleted == []


def test_upload_and_submission_claims_receive_fresh_leases(app, tmp_path, monkeypatch):
    photo_id = _prepare_photos(app, tmp_path, count=1)[0]
    fake = CountingBatchProvider()
    service = _wire_fake(app, fake)
    repository = service.batches
    leases = iter(["2099-01-01T00:00:00+00:00", "2099-01-02T00:00:00+00:00"])
    claimed: list[tuple[str, str]] = []
    original_upload = repository.claim_upload
    original_submission = repository.claim_submission
    monkeypatch.setattr(service, "_side_effect_lease_until", lambda _provider_id: next(leases))

    def claim_upload(*args):
        claimed.append(("upload", args[-1]))
        return original_upload(*args)

    def claim_submission(*args):
        claimed.append(("submission", args[-1]))
        return original_submission(*args)

    monkeypatch.setattr(repository, "claim_upload", claim_upload)
    monkeypatch.setattr(repository, "claim_submission", claim_submission)
    service.submit(scope="manual_selection", photo_ids=[photo_id], created_by="tester")
    assert claimed == [
        ("upload", "2099-01-01T00:00:00+00:00"),
        ("submission", "2099-01-02T00:00:00+00:00"),
    ]
    assert fake.upload_calls >= 1
    assert fake.create_calls >= 1
