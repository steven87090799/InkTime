from __future__ import annotations

import json
from pathlib import Path

from PIL import Image
import pytest

from scripts import openai_batch_live_smoke as smoke
from tests.unit.test_analysis_schema import valid_result


def _line(identifier="test", *, content=None, finish="stop", http=200):
    return {"custom_id": identifier, "response": {"status_code": http, "body": {
        "choices": [{"finish_reason": finish, "message": {"content": json.dumps(valid_result()) if content is None else content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 50},
                  "completion_tokens_details": {"reasoning_tokens": 20}},
    }}}


@pytest.mark.parametrize("case", ["valid", "failed", "cancelled", "expired", "partial", "missing", "duplicate",
                                  "unexpected", "invalid_json", "truncated", "http_error", "bad_analysis"])
def test_acceptance_requires_complete_current_analysis_and_exact_coverage(tmp_path, case):
    remote = {"status": "completed", "request_counts": {"total": 1, "completed": 1, "failed": 0}}
    lines = [_line()]
    if case in {"failed", "cancelled", "expired"}:
        remote["status"] = case
    elif case == "partial":
        remote["request_counts"]["failed"] = 1
    elif case == "missing":
        lines = []
    elif case == "duplicate":
        lines *= 2
    elif case == "unexpected":
        lines = [_line("other")]
    elif case == "invalid_json":
        lines = [_line(content="not json")]
    elif case == "truncated":
        lines = [_line(finish="length")]
    elif case == "http_error":
        lines = [_line(http=500)]
    elif case == "bad_analysis":
        lines = [_line(content='{"schema_version":5}')]
    path = tmp_path / "output.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    summary = smoke._validate_outputs(remote, ["test"], [path])
    assert summary["accepted"] is (case == "valid")
    if case == "valid":
        assert summary["items"][0]["analysis"]["schema_version"] == 5
        assert summary["usage"] == {"input_tokens": 100, "cached_tokens": 50, "output_tokens": 200, "reasoning_tokens": 20}


class FakeProvider:
    def __init__(self, *, status="completed", ambiguous=False):
        self.status, self.ambiguous = status, ambiguous
        self.created = self.uploaded = 0
        self.deleted = []
        self.lines = []
        self.remote = {}

    def build_analysis_request_body(self, **kwargs):
        assert kwargs["stage"] == "single" and kwargs["max_tokens"] == 512
        return {"model": kwargs["model"], "messages": [{"content": "synthetic image"}]}

    def upload_batch_file(self, path, **kwargs):
        self.uploaded += 1
        self.lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return "file-input"

    def create_batch(self, input_id, **kwargs):
        self.created += 1
        self.remote = {"id": "batch-existing", "input_file_id": input_id, "metadata": kwargs["metadata"],
                       "status": self.status, "output_file_id": "file-output",
                       "request_counts": {"total": len(self.lines), "completed": len(self.lines), "failed": 0}}
        if self.ambiguous:
            raise RuntimeError("response lost after submission")
        return self.remote.copy()

    def retrieve_batch(self, identifier):
        assert identifier == "batch-existing"
        return self.remote.copy()

    def download_file_content(self, identifier, path):
        assert identifier == "file-output"
        path.write_text("\n".join(json.dumps(_line(line["custom_id"])) for line in self.lines), encoding="utf-8")

    def delete_remote_file(self, identifier):
        self.deleted.append(identifier)

    def close(self):
        pass


@pytest.fixture
def setup_smoke(tmp_path, monkeypatch):
    image = tmp_path / "synthetic.jpg"
    Image.new("RGB", (32, 32), "white").save(image)
    for key, value in {"INKTIME_OPENAI_LIVE_SMOKE": "1", "OPENAI_API_KEY": "dummy-not-real",
                       "INKTIME_LIVE_SMOKE_IMAGES": str(image), "INKTIME_LIVE_SMOKE_MODEL": "fixture-model",
                       "INKTIME_LIVE_SMOKE_MAX_COST_USD": "1", "INKTIME_LIVE_SMOKE_INPUT_USD_PER_MILLION": "1",
                       "INKTIME_LIVE_SMOKE_OUTPUT_USD_PER_MILLION": "1", "INKTIME_LIVE_SMOKE_TIMEOUT_SECONDS": "1"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    return tmp_path / "receipt.json"


def test_pending_resume_does_not_upload_or_create_again_and_only_terminal_results_are_cleaned(setup_smoke, monkeypatch):
    path = setup_smoke
    provider = FakeProvider(status="in_progress")
    monkeypatch.setattr(smoke, "OpenAICompatibleProvider", lambda **kwargs: provider)
    times = iter([0, 2])
    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(times))
    assert smoke.main(["--receipt", str(path)]) == 3
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert receipt["remote_batch_id"] == "batch-existing" and receipt["state"] == "pending"
    assert provider.deleted == []
    assert smoke.main(["--receipt", str(path)]) == 1
    provider.remote["status"] = "completed"
    monkeypatch.setattr(smoke.time, "monotonic", lambda: 0)
    assert smoke.main(["--receipt", str(path), "--resume"]) == 0
    assert provider.created == provider.uploaded == 1
    assert set(provider.deleted) == {"file-input", "file-output"}
    assert smoke.main(["--receipt", str(path), "--resume"]) == 0
    assert len(provider.deleted) == 2
    assert "dummy-not-real" not in path.read_text(encoding="utf-8")


def test_ambiguous_create_stays_fenced_and_requires_matching_metadata_for_recovery(setup_smoke, monkeypatch):
    path = setup_smoke
    provider = FakeProvider(ambiguous=True)
    monkeypatch.setattr(smoke, "OpenAICompatibleProvider", lambda **kwargs: provider)
    assert smoke.main(["--receipt", str(path)]) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "create_started"
    assert smoke.main(["--receipt", str(path), "--resume"]) == 3
    assert provider.deleted == []
    identity = provider.remote["metadata"]["inktime_batch_id"]
    provider.remote["metadata"]["inktime_batch_id"] = "unrelated"
    assert smoke.main(["--receipt", str(path), "--resume", "--remote-batch-id", "batch-existing"]) == 1
    provider.remote["metadata"]["inktime_batch_id"] = identity
    assert smoke.main(["--receipt", str(path), "--resume", "--remote-batch-id", "batch-existing"]) == 0
    assert provider.created == provider.uploaded == 1


def test_budget_is_checked_before_remote_upload(setup_smoke, monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(smoke, "OpenAICompatibleProvider", lambda **kwargs: provider)
    monkeypatch.setenv("INKTIME_LIVE_SMOKE_MAX_COST_USD", "0.0000001")
    assert smoke.main(["--receipt", str(setup_smoke)]) == 1
    assert provider.uploaded == provider.created == 0
    assert not setup_smoke.exists()


def test_exclusive_receipt_lock_blocks_concurrent_execution(setup_smoke, monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(smoke, "OpenAICompatibleProvider", lambda **kwargs: provider)
    lock = Path(str(setup_smoke) + ".lock")
    lock.write_text("owned by another process", encoding="utf-8")
    assert smoke.main(["--receipt", str(setup_smoke)]) == 1
    assert provider.uploaded == provider.created == 0
    assert lock.exists()
