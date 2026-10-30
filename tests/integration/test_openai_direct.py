from __future__ import annotations

import json

import pytest

from inktime.app.providers.openai_compatible import ProviderHTTPError
from inktime.app.providers.openai_responses import OpenAIResponsesProvider
from inktime.app.services.batch_analysis import BatchAnalysisService, BatchLifecycleError
from inktime.app.services.openai_costs import openai_cost_projection
from tests.conftest import create_admin, login
from tests.integration.test_analysis_pipeline import prepare
from tests.unit.test_batch_analysis_lifecycle import FakeBatchProvider, _prepare_photos, _wire_fake
from tests.unit.test_openai_responses import Session, provider, wire_response


def save_direct(app, **updates):
    return app.extensions["inktime_provider_repository"].save(
        {"id": "direct-test", "name": "OpenAI Direct", "kind": "openai",
         "base_url": "https://api.openai.com/v1", "api_key": "sk-native-private", "priority": 1, **updates}, "tester")


def test_independent_encrypted_keys_defaults_and_primary_billing_boundary(app, client):
    create_admin(app)
    login(client)
    repository = app.extensions["inktime_provider_repository"]
    router_id = repository.save({"name": "OpenRouter", "kind": "openrouter", "api_key": "sk-router-private",
                                 "base_url": "https://openrouter.ai/api/v1", "model": "openrouter/free", "priority": 2}, "tester")
    direct_id = save_direct(app)
    direct = repository.get(direct_id)
    assert direct["model"] == "gpt-6-luna" and direct["options"] == {"api_mode": "responses", "reasoning_effort": "none"}
    assert repository.get(router_id, include_secret=True)["api_key"] == "sk-router-private"
    assert repository.get(direct_id, include_secret=True)["api_key"] == "sk-native-private"
    assert b"sk-native-private" not in app.extensions["inktime_database"].path.read_bytes()
    assert repository.pricing(direct_id)["gpt-6-luna"]["input_per_million"] == .1
    route = app.extensions["inktime_provider_service"].build_router()
    try:
        assert len(route.channels) == 1
        assert isinstance(route.channels[0].provider, OpenAIResponsesProvider)
    finally:
        route.close()
    page = client.get("/providers").get_data(as_text=True)
    assert "OpenAI Billing" in page and "openai_organization" in page
    assert "sk-native-private" not in page and "sk-router-private" not in page
    with pytest.raises(ValueError, match="另外提供"):
        repository.save({"id": router_id, "kind": "openai", "name": "changed",
                         "base_url": "https://api.openai.com/v1", "model": "gpt-6-luna"}, "tester")


def test_existing_openai_chat_configuration_keeps_frozen_revision(app):
    repository = app.extensions["inktime_provider_repository"]
    # Simulate an existing deployment, whose options predate Responses.
    save_direct(app, options={"api_mode": "chat_completions"}, model="gpt-5.6-luna")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE providers SET options_json='{}' WHERE id='direct-test'")
    service = app.extensions["inktime_provider_service"]
    before = service.config_revision(repository.get("direct-test"))
    route = service.build_router()
    try:
        assert route.channels[0].provider.api_mode == "chat_completions"
        assert service.config_revision(repository.get("direct-test")) == before
    finally:
        route.close()


def test_native_plan_freezes_independent_model_and_reasoning(app):
    save_direct(app, options={"api_mode": "responses", "reasoning_effort": "low"})
    route = app.extensions["inktime_provider_service"].usable_route_snapshot()
    plan = app.extensions["inktime_analysis_service"].build_plan(
        strategy="single", provider_route=route, scoring_profile_id="")
    assert plan["model"] == "gpt-6-luna" and plan["reasoning_effort"] == "low"


def test_legacy_openrouter_kind_canonicalization_keeps_its_own_key(app):
    repository = app.extensions["inktime_provider_repository"]
    provider_id = repository.save({"name": "Legacy OpenRouter", "kind": "openrouter",
                                   "base_url": "https://openrouter.ai/api/v1", "model": "openrouter/free",
                                   "api_key": "router-legacy-key"}, "tester")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE providers SET kind='openai_compatible' WHERE id=?", (provider_id,))
    repository.save({"id": provider_id, "name": "Legacy OpenRouter", "kind": "openrouter",
                     "base_url": "https://openrouter.ai/api/v1", "model": "openrouter/free"}, "tester")
    assert repository.get(provider_id, include_secret=True)["api_key"] == "router-legacy-key"


def test_account_model_probe_is_read_only_and_never_returns_key(app, client, monkeypatch):
    create_admin(app)
    login(client)
    direct_id = save_direct(app)
    monkeypatch.setattr(OpenAIResponsesProvider, "available_models",
                        lambda _: [{"id": "gpt-6-luna", "available": True}])
    response = client.get(f"/api/v1/providers/{direct_id}/models")
    assert response.status_code == 200
    assert response.json["billing_balance"] is None
    assert response.json["billing_message"] == "需至 OpenAI Billing 查詢"
    assert "sk-native-private" not in response.get_data(as_text=True)


def test_responses_photo_pipeline_persists_original_contract_and_measured_cost(app, tmp_path):
    save_direct(app)
    _, ids, analysis = prepare(app, tmp_path)
    p = provider(options={"api_mode": "responses"})
    result = analysis.analyze_photo(photo_id=ids[0], job_id=None, provider=p, strategy="high_quality", high_model="gpt-6-luna")
    assert result["analysis"]["side_caption"]
    with app.extensions["inktime_database"].session() as connection:
        usage = connection.execute("SELECT * FROM api_usage").fetchone()
        stored = connection.execute("SELECT * FROM photo_analysis WHERE photo_id=?", (ids[0],)).fetchone()
        assert usage["input_tokens"] == 1000 and usage["reasoning_tokens"] == 40
        assert usage["estimated_cost"] == pytest.approx(.00015)
        assert usage["actual_cost"] is None and usage["cost_source"] == "estimated"
        assert stored["schema_version"] == 6
    projection = openai_cost_projection(app.extensions["inktime_database"], remaining=50)
    assert projection["sample_photos"] == 1 and projection["basis"] == "same_openai_model"
    assert projection["scenarios"] == [{"photos": 10_000, "standard": pytest.approx(1.5), "batch": pytest.approx(.75)},
                                       {"photos": 100_000, "standard": pytest.approx(15), "batch": pytest.approx(7.5)}]
    assert projection["remaining_standard"] == pytest.approx(.0075)
    assert len(p.session.calls) == 1


@pytest.mark.parametrize(("status", "error_code"), [(429, "credit_balance_exhausted"),
                         (429, "rate_limit_exceeded"), (401, "invalid_api_key"), (404, "model_not_found")])
def test_credit_rejected_photo_can_resume_without_rebilling_an_unknown_request(app, tmp_path, status, error_code):
    _, ids, analysis = prepare(app, tmp_path)
    p = provider(Session({"error": {"code": error_code}}, status=status))
    with pytest.raises(ProviderHTTPError):
        analysis.analyze_photo(photo_id=ids[0], job_id=None, provider=p, strategy="high_quality", high_model="gpt-6-luna")
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT state FROM billable_operations").fetchone()[0] == "not_sent"
        assert connection.execute("SELECT COUNT(*) FROM api_usage WHERE cost_source='unknown'").fetchone()[0] == 0
    p.session = Session()
    result = analysis.analyze_photo(photo_id=ids[0], job_id=None, provider=p, strategy="high_quality", high_model="gpt-6-luna")
    assert result["analysis"]["side_caption"]
    assert len(p.session.calls) == 1


class ResponsesBatch(FakeBatchProvider):
    fail_first = False

    def build_analysis_request_body(self, **kwargs):
        return provider().build_analysis_request_body(**kwargs)

    def retrieve_batch(self, batch_id):
        return {**super().retrieve_batch(batch_id), "endpoint": "/v1/responses"}

    def download_file_content(self, file_id, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for index, custom_id in enumerate(reversed(self.custom_ids)):
            body = wire_response(**({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
                                    if self.fail_first and index == 0 else {}))
            lines.append(json.dumps({"custom_id": custom_id, "response": {"status_code": 200, "body": body}}))
        destination.write_text("\n".join(lines) + "\n")
        return destination


def batch_setup(app):
    fake = ResponsesBatch()
    service = _wire_fake(app, fake)
    app.extensions["inktime_provider_repository"].save(
        {"id": "fake-batch", "name": "OpenAI Batch", "kind": "openai", "api_key": "batch-private-key",
         "base_url": "https://api.openai.com/v1", "model": "gpt-6-luna", "supports_batch": True,
         "options": {"api_mode": "responses"}}, "tester")
    return service, fake


def test_responses_batch_requires_private_photo_consent_before_creating_work(app, tmp_path):
    _prepare_photos(app, tmp_path, 1)
    service, fake = batch_setup(app)
    with pytest.raises(BatchLifecycleError, match="同意"):
        service.submit(sample_count=1)
    assert not fake.custom_ids
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM analysis_batches").fetchone()[0] == 0


@pytest.mark.parametrize("partial", [False, True])
def test_responses_batch_unordered_partial_import_restart_and_retry(app, tmp_path, partial):
    photo_ids = _prepare_photos(app, tmp_path, 3)
    service, fake = batch_setup(app)
    fake.fail_first = partial
    result = service.submit(sample_count=3, privacy_acknowledged=True)
    batch_id = result["batch_ids"][0]
    batch = service.batches.get(batch_id)
    assert batch["endpoint"] == "/v1/responses"
    job = service.jobs.get(batch["job_id"])
    assert json.loads(job["settings_json"])["batch_privacy_acknowledged"] is True
    for line in open(batch["local_input_path"]):
        body = json.loads(line)
        assert body["url"] == "/v1/responses" and body["body"]["store"] is False
    # Rebuild the service using persisted SQLite state before polling/import.
    restarted = BatchAnalysisService(**{name: getattr(service, name) for name in (
        "database", "batches", "jobs", "job_service", "photos", "providers", "provider_service", "usage",
        "thumbnails", "analysis", "settings", "data_dir", "observability", "scoring_repository")})
    assert restarted.poll_due(limit=10)["polled"] == 1
    imported = restarted.import_batch(batch_id)
    assert imported["success"] == len(photo_ids) - int(partial), [
        (item["status"], item["error_code"], item["error_message"]) for item in restarted.batches.items(batch_id)]
    restarted.import_batch(batch_id)
    with service.database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM api_usage WHERE batch_id=?", (batch_id,)).fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM photo_analysis").fetchone()[0] == 3 - int(partial)
    if partial:
        fake.fail_first = False
        retry = restarted.retry_failed(batch_id)
        assert retry["candidate_count"] == 1
        assert len(fake.custom_ids) == 1


def test_cost_projection_with_no_measurements_shows_no_invented_balance_or_amount(app, client):
    create_admin(app)
    login(client)
    response = client.get("/api/v1/costs/openai-projection?remaining=100000")
    assert response.status_code == 200
    assert response.json["scenarios"] == [] and response.json["billing_balance"] is None
    assert response.json["standard_per_photo"] is None
    assert client.get("/costs").status_code == 200
    assert client.get("/api/v1/costs/openai-projection?remaining=-1").status_code == 400


def test_batch_credit_exhaustion_stops_future_shards_and_manual_resume_preserves_them(app, tmp_path, monkeypatch):
    _prepare_photos(app, tmp_path, 3)
    service, fake = batch_setup(app)
    monkeypatch.setattr(service, "_batch_limits", lambda: (1, 150 * 1024 * 1024))
    counts = {"uploads": 0, "creates": 0}
    original_upload, original_create = fake.upload_batch_file, fake.create_batch

    def upload(*args, **kwargs):
        counts["uploads"] += 1
        kwargs.pop("remote_filename", None)
        return original_upload(*args, **kwargs)

    def create(*args, **kwargs):
        counts["creates"] += 1
        raise ProviderHTTPError("credits exhausted", "API_CREDIT_EXHAUSTED", http_status=429)

    monkeypatch.setattr(fake, "upload_batch_file", upload)
    monkeypatch.setattr(fake, "create_batch", create)
    estimate = service.estimate(sample_count=3)
    result = service.submit(sample_count=3, privacy_acknowledged=True)
    assert counts == {"uploads": 1, "creates": 1}
    assert len(result["prepared_batch_ids"]) == 3
    shards = [service.batches.get(batch_id) for batch_id in result["prepared_batch_ids"]]
    assert [row["total_items"] for row in shards] == [1, 1, 1]
    assert sum(row["estimated_cost"] for row in shards) == pytest.approx(estimate["estimated_cost"])
    job_id = result["job_id"]
    assert service.jobs.get(job_id)["status"] in {"paused", "pausing"}
    service.poll_due(limit=10)
    assert counts == {"uploads": 1, "creates": 1}
    remaining = [service.batches.get(batch_id) for batch_id in result["prepared_batch_ids"][1:]]
    assert all(row["status"] == "preparing" and row["input_file_id"] is None for row in remaining)
    monkeypatch.setattr(fake, "create_batch", original_create)
    assert service.jobs.transition(job_id, {"paused", "pausing"}, "running", "manual_resume")
    service.poll_due(limit=10)
    assert counts["uploads"] == 3
