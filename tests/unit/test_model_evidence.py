from __future__ import annotations

import base64
from hashlib import sha256
import json

import pytest

from inktime.app.db import Database, migrate
from inktime.app.repositories import model_evidence
from inktime.app.repositories.billable_operations import BillableOperationRepository, UnreconciledOperationError
from inktime.app.providers.base import ProviderResponse, Usage
from inktime.app.services.backups import BackupService


def test_exact_model_input_and_untruncated_output_survive_database_backup(tmp_path):
    database = Database(tmp_path / "data" / "inktime.db")
    database.path.parent.mkdir()
    migrate(database)
    operation, _ = BillableOperationRepository(database).begin("original-photo-sha", "vision-plan")
    image = b"exact JPEG bytes sent to the provider"
    uri = "data:image/jpeg;base64," + base64.b64encode(image).decode()
    model_evidence.save_model_request(database, operation, {
        "model": "test", "messages": [{"content": [{"image_url": {"url": uri}}, {"text": "complete prompt"}]}]
    })
    raw = json.dumps({"choices": [{"message": {"content": "完整回應" * 9000}}]}, ensure_ascii=False)
    model_evidence.save_model_transport_response(database, operation, raw)
    backup = BackupService(database, tmp_path / "backups")
    archive = backup.create()
    manifest = backup.validate(archive)
    assert manifest["important_table_counts"]["model_request_evidence"] == 1
    with database.session() as connection:
        row = connection.execute("SELECT * FROM model_request_evidence WHERE operation_id=?", (operation,)).fetchone()
    assert row["response_json"] == raw
    request = json.loads(row["request_json"])
    reference = request["messages"][0]["content"][0]["image_url"]["url"]["_inktime_image"]
    assert (database.path.parent / reference["path"]).read_bytes() == image
    assert reference["sha256"] == sha256(image).hexdigest()
    assert request["messages"][0]["content"][1]["text"] == "complete prompt"
    assert "base64" not in row["request_json"]


def test_request_storage_failure_is_definitely_not_sent(tmp_path, monkeypatch):
    database = Database(tmp_path / "inktime.db")
    migrate(database)
    def no_space(*_args):
        raise OSError("no space")
    monkeypatch.setattr(model_evidence, "_archive_image", no_space)
    with pytest.raises(model_evidence.ModelEvidenceNotSavedError) as error:
        model_evidence.save_model_request(database, "operation", {"image": "data:image/jpeg;base64,YQ=="})
    assert error.value.not_sent
    with database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_request_evidence").fetchone()[0] == 0


def test_missing_response_evidence_is_ambiguous_and_fences_retry(tmp_path):
    database = Database(tmp_path / "inktime.db")
    migrate(database)
    operations = BillableOperationRepository(database)
    operation, _ = operations.begin("photo-sha", "vision-plan")
    with pytest.raises(model_evidence.ModelResponseNotSavedError) as error:
        model_evidence.save_model_transport_response(database, operation, "provider response")
    assert error.value.ambiguous and error.value.vision_started
    with pytest.raises(UnreconciledOperationError):
        operations.begin("photo-sha", "vision-plan")


def test_completed_paid_checkpoint_survives_cache_loss_and_restart(tmp_path):
    database = Database(tmp_path / "inktime.db")
    migrate(database)
    operations = BillableOperationRepository(database)
    operation, _ = operations.begin("photo-sha", "vision-plan")
    response = ProviderResponse("complete description", Usage(100, 20))
    operations.save_response(operation, response)
    operations.finish(operation)
    restarted = BillableOperationRepository(Database(database.path))
    assert restarted.check_retry("photo-sha", "vision-plan", reuse_completed=True)
    assert restarted.begin("photo-sha", "vision-plan", reuse_completed=True) == (operation, response)
