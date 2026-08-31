from __future__ import annotations

from contextlib import contextmanager
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
from uuid import uuid4

from PIL import Image

from inktime.app.api import photos as photos_api
from inktime.app.domain.analysis.plan import fingerprint
from inktime.app.domain.photos import PhotoPreprocessor
from inktime.app.providers.base import ProviderResponse, Usage, VisionProvider
from inktime.app.providers.router import FailoverVisionProvider, ProviderChannel
from inktime.app.services import analysis as analysis_module
from inktime.app.workers.scanner import PhotoScanner
from tests.conftest import create_admin, csrf, login
from tests.integration.test_jobs import add_photos
from tests.unit.test_analysis_schema import valid_result


class CountingProvider(VisionProvider):
    name = "Counting Provider"

    def __init__(self, result=None):
        self.result = result or valid_result()
        self.analyze_calls = 0

    def analyze(self, **_kwargs):
        self.analyze_calls += 1
        return ProviderResponse(json.dumps(self.result, ensure_ascii=False), Usage(100, 20, 0))

    def repair_json(self, **_kwargs):
        raise AssertionError("valid JSON should not need repair")

    def submit_batch(self, requests, completion_window="24h"):
        return "batch"

    def poll_batch(self, batch_id):
        return {"status": "completed"}

    def cancel_batch(self, batch_id):
        return {"status": "cancelled"}

    def estimate_cost(self, model, usage):
        return (usage.input_tokens + usage.output_tokens) / 1_000_000

    def validate_config(self):
        return True, "ok"


class SlowCountingProvider(CountingProvider):
    def analyze(self, **kwargs):
        time.sleep(0.1)
        return super().analyze(**kwargs)


def _scan(app, tmp_path, *, screenshot=False, duplicate=False):
    root = tmp_path / "photos"
    root.mkdir()
    filename = "螢幕快照.png" if screenshot else "photo.jpg"
    Image.effect_noise((900, 600), 90).convert("RGB").save(root / filename)
    if duplicate:
        (root / "copy.jpg").write_bytes((root / filename).read_bytes())
    scanned = PhotoScanner(
        app.extensions["inktime_photo_repository"],
        PhotoPreprocessor(),
        app.extensions["inktime_thumbnail_cache"],
    ).scan("測試照片", root, build_thumbnails=False)
    with app.extensions["inktime_database"].session() as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT id FROM photos WHERE library_id=? ORDER BY relative_path",
                (scanned["library_id"],),
            )
        ]


def _setting(app, key, value):
    app.extensions["inktime_settings_repository"].update(key, value, changed_by="test", source_ip="127.0.0.1")


def _manual_excluded_photo(app, tmp_path, *, actor: str) -> str:
    photo_id = _scan(app, tmp_path)[0]
    app.extensions["inktime_photo_repository"].set_exclusion(
        photo_id, action="exclude", changed_by=actor
    )
    return photo_id


def _enable_fake_usable_provider(app) -> str:
    """Configure a usable route without making any external Provider request."""

    return app.extensions["inktime_provider_repository"].save(
        {
            "name": "CI fake Vision Provider",
            "kind": "openai_compatible",
            "base_url": "https://provider.invalid/v1",
            "api_key": "test-provider-secret",
            "enabled": True,
        },
        user_id="test",
    )


def test_excluded_photo_is_shown_and_restore_is_selectable(client, app, tmp_path):
    user_id = create_admin(app)
    login(client)
    photo_id = _scan(app, tmp_path, screenshot=True)[0]

    page = client.get("/photos/excluded")
    assert page.status_code == 200
    assert "螢幕快照.png" in page.get_data(as_text=True)

    restored = app.extensions["inktime_photo_repository"].set_exclusion(
        photo_id, action="restore", changed_by=user_id
    )
    assert restored["eligible"] == 1
    assert photo_id in app.extensions["inktime_photo_repository"].eligible_photo_ids()
    assert photo_id not in {
        str(row["id"]) for row in app.extensions["inktime_photo_repository"].search_exclusions()
    }


def test_manual_restore_does_not_immediately_reexclude(app, tmp_path):
    user_id = create_admin(app)
    photo_id = _scan(app, tmp_path, screenshot=True)[0]
    repository = app.extensions["inktime_photo_repository"]
    repository.set_exclusion(photo_id, action="restore", changed_by=user_id)
    unchanged = repository.set_exclusion(photo_id, action="reanalyze", changed_by=user_id)
    assert unchanged["exclusion_status"] == "manually_restored"
    assert unchanged["manual_override"] == 1


def test_background_reanalysis_preserves_automatic_exclusion(app, tmp_path):
    actor = create_admin(app)
    photo_id = _scan(app, tmp_path, screenshot=True)[0]
    repository = app.extensions["inktime_photo_repository"]
    before = repository.get_with_path(photo_id)

    after = repository.set_exclusion(photo_id, action="reanalyze", changed_by=actor)

    assert after["eligible"] == before["eligible"] == 0
    assert after["exclusion_status"] == before["exclusion_status"] == "auto_excluded"
    assert after["reject_reason"] == before["reject_reason"]
    assert after["reject_details_json"] == before["reject_details_json"]


def test_reapply_rules_never_restores_manual_exclusion(app, tmp_path):
    actor = create_admin(app)
    photo_id = _scan(app, tmp_path)[0]
    repository = app.extensions["inktime_photo_repository"]
    repository.set_exclusion(photo_id, action="exclude", changed_by=actor)
    before = repository.get_with_path(photo_id)

    after = repository.set_exclusion(
        photo_id,
        action="reanalyze",
        changed_by=actor,
        reapply_rules=True,
    )

    assert after["eligible"] == 0
    assert after["exclusion_status"] == "manually_excluded"
    assert after["reject_reason"] == before["reject_reason"]


def test_ai_off_does_not_call_provider(app, tmp_path):
    photo_id = _scan(app, tmp_path)[0]
    _setting(app, "analysis.ai_mode", "off")
    provider = CountingProvider()
    result = app.extensions["inktime_analysis_service"].analyze_photo(
        photo_id=photo_id, job_id=None, provider=provider, strategy="high_quality", high_model="test"
    )
    assert result["stage"] == "local_fallback"
    assert provider.analyze_calls == 0


def test_force_ai_calls_provider_when_ai_is_off_and_preserves_exclusion_audit(app, tmp_path):
    actor = create_admin(app)
    photo_id = _manual_excluded_photo(app, tmp_path, actor=actor)
    repository = app.extensions["inktime_photo_repository"]
    before = repository.get_with_path(photo_id)
    _setting(app, "analysis.ai_mode", "off")
    _setting(app, "analysis.execution_mode", "local_with_manual_ai")
    provider = CountingProvider()
    provider.provider_id = "provider-force-test"
    plan = app.extensions["inktime_analysis_service"].build_plan(
        strategy="high_quality",
        provider_route=[],
        scoring_profile=dict(app.extensions["inktime_scoring_repository"].current()),
    )
    job_id = app.extensions["inktime_job_service"].create_analysis_job(
        name="force excluded",
        strategy="high_quality",
        settings={"force_ai": True},
        created_by=actor,
        budget_limit=None,
        photo_ids=[photo_id],
        analysis_fingerprint=fingerprint(plan),
        force_recompute=True,
        analysis_spec=plan,
    )

    result = app.extensions["inktime_analysis_service"].analyze_photo(
        photo_id=photo_id,
        job_id=job_id,
        provider=provider,
        strategy="high_quality",
        force_ai=True,
        force_actor=actor,
        force_recompute=True,
        analysis_plan=plan,
    )

    assert result["stage"] == "single"
    assert provider.analyze_calls == 1
    after = repository.get_with_path(photo_id)
    assert after["eligible"] == before["eligible"] == 0
    assert after["exclusion_status"] == before["exclusion_status"] == "manually_excluded"
    assert after["reject_reason"] == before["reject_reason"]
    with app.extensions["inktime_database"].session() as connection:
        analysis = connection.execute(
            "SELECT job_id,analysis_fingerprint FROM photo_analysis WHERE photo_id=? ORDER BY id DESC LIMIT 1",
            (photo_id,),
        ).fetchone()
        event = connection.execute(
            "SELECT changes_json,changed_by FROM photo_events WHERE photo_id=? AND event='force_ai_analysis_completed' "
            "ORDER BY id DESC LIMIT 1",
            (photo_id,),
        ).fetchone()
    assert analysis["job_id"] == job_id
    assert analysis["analysis_fingerprint"]
    assert event["changed_by"] == actor
    audit = json.loads(event["changes_json"])
    assert audit["job_id"] == job_id
    assert audit["provider_id"] == "provider-force-test"
    assert audit["provider_name"] == "Counting Provider"
    assert audit["model"] == plan["model"]


def test_confirmed_screenshot_cannot_cross_force_ai_send_boundary(app, tmp_path):
    actor = create_admin(app)
    photo_id = _scan(app, tmp_path, screenshot=True)[0]
    _setting(app, "analysis.execution_mode", "local_with_manual_ai")
    provider = CountingProvider()

    result = app.extensions["inktime_analysis_service"].analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=provider,
        strategy="high_quality",
        force_ai=True,
        force_actor=actor,
        force_recompute=True,
    )

    assert result["stage"] == "prefilter"
    assert provider.analyze_calls == 0


def test_force_ai_api_is_admin_exclusion_only_and_creates_fresh_job(client, app, tmp_path):
    actor = create_admin(app)
    login(client)
    _enable_fake_usable_provider(app)
    excluded_id = _manual_excluded_photo(app, tmp_path, actor=actor)
    root = tmp_path / "eligible-parent"
    root.mkdir()
    eligible_id = _scan(app, root)[0]
    assert eligible_id != excluded_id
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE photos SET eligible=1,exclusion_status='eligible',manual_override=0 WHERE id=?",
            (eligible_id,),
        )
    _setting(app, "analysis.ai_mode", "off")
    _setting(app, "analysis.execution_mode", "local_with_manual_ai")
    headers = {"X-CSRF-Token": csrf(client)}
    forbidden = client.post(f"/api/v1/photos/{eligible_id}/ai", headers=headers)
    assert forbidden.status_code == 403

    queued = client.post(f"/api/v1/photos/{excluded_id}/ai", headers=headers)
    assert queued.status_code == 201
    job = app.extensions["inktime_job_repository"].get(queued.json["id"])
    assert json.loads(job["settings_json"])["force_ai"] is True
    assert job["force_recompute"] == 1
    assert job["analysis_fingerprint"]
    assert [item["photo_id"] for item in app.extensions["inktime_job_repository"].list_items(job["id"])] == [
        excluded_id
    ]


def test_confirmed_screenshot_ai_api_is_controlled_and_creates_no_job(client, app, tmp_path):
    create_admin(app)
    login(client)
    _enable_fake_usable_provider(app)
    photo_id = _scan(app, tmp_path, screenshot=True)[0]
    _setting(app, "analysis.execution_mode", "local_with_manual_ai")
    with app.extensions["inktime_database"].session() as connection:
        before_jobs = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    response = client.post(
        f"/api/v1/photos/{photo_id}/ai", headers={"X-CSRF-Token": csrf(client)}
    )

    assert response.status_code == 409
    assert {key: response.json[key] for key in ("error_code", "message")} == {
        "error_code": "VLM-008",
        "message": "已確認為截圖；為保護隱私與額度，禁止送入 AI 模型",
    }
    assert response.json["user_error"]["title"] == "這張照片不允許傳送給模型"
    with app.extensions["inktime_database"].session() as connection:
        assert int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]) == before_jobs


def test_manual_ai_idempotency_key_is_namespaced_per_endpoint(client, app, tmp_path):
    actor = create_admin(app)
    login(client)
    _enable_fake_usable_provider(app)
    excluded_id = _manual_excluded_photo(app, tmp_path, actor=actor)
    _setting(app, "analysis.ai_mode", "off")
    _setting(app, "analysis.execution_mode", "local_with_manual_ai")
    headers = {"X-CSRF-Token": csrf(client), "Idempotency-Key": "shared-manual-key"}

    single = client.post(f"/api/v1/photos/{excluded_id}/ai", headers=headers)
    batch = client.post(
        "/api/v1/photos/exclusions/ai",
        json={"photo_ids": [excluded_id]},
        headers=headers,
    )

    assert single.status_code == 201
    assert batch.status_code == 201
    assert single.json["id"] != batch.json["id"]


def test_manual_ai_idempotency_conflict_has_stable_api_error_code(client, app, tmp_path):
    actor = create_admin(app)
    login(client)
    _enable_fake_usable_provider(app)
    first_root = tmp_path / "first-excluded"
    second_root = tmp_path / "second-excluded"
    first_root.mkdir()
    second_root.mkdir()
    excluded_ids = [
        _manual_excluded_photo(app, first_root, actor=actor),
        _manual_excluded_photo(app, second_root, actor=actor),
    ]
    assert excluded_ids[0] != excluded_ids[1]
    _setting(app, "analysis.ai_mode", "off")
    _setting(app, "analysis.execution_mode", "local_with_manual_ai")
    headers = {"X-CSRF-Token": csrf(client), "Idempotency-Key": "manual-conflict-key"}

    first = client.post(f"/api/v1/photos/{excluded_ids[0]}/ai", headers=headers)
    second = client.post(f"/api/v1/photos/{excluded_ids[1]}/ai", headers=headers)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json["error_code"] == "IDEMPOTENCY_CONFLICT"


def test_single_excluded_photo_ai_without_provider_is_controlled_and_side_effect_free(
    client, app, tmp_path
):
    actor = create_admin(app)
    login(client)
    excluded_id = _manual_excluded_photo(app, tmp_path, actor=actor)
    _setting(app, "analysis.ai_mode", "off")
    _setting(app, "analysis.execution_mode", "local_with_manual_ai")
    with app.extensions["inktime_database"].session() as connection:
        before_jobs = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    response = client.post(
        f"/api/v1/photos/{excluded_id}/ai", headers={"X-CSRF-Token": csrf(client)}
    )
    batch_response = client.post(
        "/api/v1/photos/exclusions/ai",
        json={"photo_ids": [excluded_id]},
        headers={"X-CSRF-Token": csrf(client)},
    )
    _setting(app, "analysis.execution_mode", "automatic_ai")
    _setting(app, "analysis.ai_mode", "top_candidates")
    automatic_response = client.post(
        "/api/v1/photos/ai/run",
        json={},
        headers={"X-CSRF-Token": csrf(client)},
    )

    for rejected in (response, batch_response, automatic_response):
        assert rejected.status_code == 409
        assert {key: rejected.json[key] for key in ("error_code", "message")} == {
            "error_code": "VLM-008",
            "message": "目前沒有已啟用且設定完整的 Vision Provider",
        }
        assert "模型設定" in rejected.json["user_error"]["title"]
    with app.extensions["inktime_database"].session() as connection:
        assert int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]) == before_jobs


def test_ai_cache_hit_does_not_call_provider_twice(app, tmp_path, monkeypatch):
    first_id = _scan(app, tmp_path)[0]
    _setting(app, "analysis.ai_mode", "eligible")
    first = CountingProvider()
    service = app.extensions["inktime_analysis_service"]
    service.analyze_photo(
        photo_id=first_id, job_id=None, provider=first, strategy="high_quality", high_model="test"
    )
    second = CountingProvider()
    cache = app.extensions["inktime_thumbnail_cache"]
    calls = 0
    original = cache.acquire_for_use

    @contextmanager
    def counted_thumbnail(*args, **kwargs):
        nonlocal calls
        calls += 1
        with original(*args, **kwargs) as path:
            yield path

    monkeypatch.setattr(cache, "acquire_for_use", counted_thumbnail)
    cached = service.analyze_photo(
        photo_id=first_id, job_id=None, provider=second, strategy="high_quality", high_model="test"
    )
    assert cached["stage"] == "cache"
    assert second.analyze_calls == 0
    assert calls == 0


def test_circuit_open_provider_cache_is_used_before_failover(app, tmp_path):
    photo_id = _scan(app, tmp_path)[0]
    _setting(app, "analysis.ai_mode", "eligible")
    _setting(app, "analysis.prefilter_enabled", False)
    service = app.extensions["inktime_analysis_service"]
    first = CountingProvider()
    first.provider_id = "provider-a"
    fallback = CountingProvider()
    fallback.provider_id = "provider-b"
    service.analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=first,
        strategy="high_quality",
        high_model="route-cache",
    )
    channel = ProviderChannel(first, priority=1, max_concurrency=1)
    channel.circuit_until = time.monotonic() + 60
    router = FailoverVisionProvider([channel, ProviderChannel(fallback, priority=2)])

    result = service.analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=router,
        strategy="high_quality",
        high_model="route-cache",
    )

    assert result["stage"] == "cache"
    assert first.analyze_calls == 1
    assert fallback.analyze_calls == 0
    assert not channel.request_times
    assert channel.semaphore.acquire(blocking=False) is True
    channel.semaphore.release()


def test_rate_limited_provider_cache_is_used_without_a_network_permit(app, tmp_path):
    photo_id = _scan(app, tmp_path)[0]
    _setting(app, "analysis.ai_mode", "eligible")
    _setting(app, "analysis.prefilter_enabled", False)
    service = app.extensions["inktime_analysis_service"]
    first = CountingProvider()
    first.provider_id = "provider-a"
    fallback = CountingProvider()
    fallback.provider_id = "provider-b"
    service.analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=first,
        strategy="high_quality",
        high_model="rate-cache",
    )
    channel = ProviderChannel(first, priority=1, max_concurrency=1, requests_per_minute=1)
    channel.request_times.append(time.monotonic())
    router = FailoverVisionProvider([channel, ProviderChannel(fallback, priority=2)])

    result = service.analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=router,
        strategy="high_quality",
        high_model="rate-cache",
    )

    assert result["stage"] == "cache"
    assert first.analyze_calls == 1
    assert fallback.analyze_calls == 0
    assert len(channel.request_times) == 1
    assert channel.semaphore.acquire(blocking=False) is True
    channel.semaphore.release()


def test_token_limited_provider_cache_is_used_without_failover(app, tmp_path):
    photo_id = _scan(app, tmp_path)[0]
    _setting(app, "analysis.ai_mode", "eligible")
    _setting(app, "analysis.prefilter_enabled", False)
    service = app.extensions["inktime_analysis_service"]
    first = CountingProvider()
    first.provider_id = "provider-a"
    fallback = CountingProvider()
    fallback.provider_id = "provider-b"
    service.analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=first,
        strategy="high_quality",
        high_model="token-cache",
    )
    channel = ProviderChannel(first, priority=1, max_concurrency=1, tokens_per_minute=1)
    channel.token_events.append((time.monotonic(), 1))
    router = FailoverVisionProvider([channel, ProviderChannel(fallback, priority=2)])

    result = service.analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=router,
        strategy="high_quality",
        high_model="token-cache",
    )

    assert result["stage"] == "cache"
    assert first.analyze_calls == 1
    assert fallback.analyze_calls == 0
    assert len(channel.token_events) == 1
    assert channel.semaphore.acquire(blocking=False) is True
    channel.semaphore.release()


def test_failover_checks_next_provider_cache_after_first_network_miss(app, tmp_path):
    photo_id = _scan(app, tmp_path)[0]
    _setting(app, "analysis.ai_mode", "eligible")
    _setting(app, "analysis.prefilter_enabled", False)
    service = app.extensions["inktime_analysis_service"]
    first = CountingProvider()
    first.provider_id = "provider-a"
    fallback = CountingProvider()
    fallback.provider_id = "provider-b"
    service.analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=fallback,
        strategy="high_quality",
        high_model="failover-cache",
    )
    channel = ProviderChannel(first, priority=1)
    channel.circuit_until = time.monotonic() + 60
    router = FailoverVisionProvider([channel, ProviderChannel(fallback, priority=2)])

    result = service.analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=router,
        strategy="high_quality",
        high_model="failover-cache",
    )

    assert result["stage"] == "cache"
    assert first.analyze_calls == 0
    assert fallback.analyze_calls == 1


def test_network_unavailable_provider_fails_over_after_cache_miss(app, tmp_path):
    photo_id = _scan(app, tmp_path)[0]
    _setting(app, "analysis.ai_mode", "eligible")
    _setting(app, "analysis.prefilter_enabled", False)
    service = app.extensions["inktime_analysis_service"]
    first = CountingProvider()
    first.provider_id = "provider-a"
    fallback = CountingProvider()
    fallback.provider_id = "provider-b"
    channel = ProviderChannel(first, priority=1, requests_per_minute=1)
    channel.request_times.append(time.monotonic())
    router = FailoverVisionProvider([channel, ProviderChannel(fallback, priority=2)])

    result = service.analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=router,
        strategy="high_quality",
        high_model="network-failover",
    )

    assert result["stage"] == "single"
    assert first.analyze_calls == 0
    assert fallback.analyze_calls == 1
    with app.extensions["inktime_database"].session() as connection:
        reserved = connection.execute(
            "SELECT count(*) FROM ai_cache_reservations WHERE status='reserved'"
        ).fetchone()[0]
    assert reserved == 0


def test_concurrent_requests_share_one_provider_call_and_thumbnail(app, tmp_path, monkeypatch):
    photo_id = _scan(app, tmp_path)[0]
    _setting(app, "analysis.ai_mode", "eligible")
    _setting(app, "analysis.prefilter_enabled", False)
    service = app.extensions["inktime_analysis_service"]
    provider = SlowCountingProvider()
    cache = app.extensions["inktime_thumbnail_cache"]
    calls = 0
    original = cache.acquire_for_use

    @contextmanager
    def counted_thumbnail(*args, **kwargs):
        nonlocal calls
        calls += 1
        with original(*args, **kwargs) as path:
            yield path

    monkeypatch.setattr(cache, "acquire_for_use", counted_thumbnail)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _ignored: service.analyze_photo(
                    photo_id=photo_id,
                    job_id=None,
                    provider=provider,
                    strategy="high_quality",
                    high_model="concurrent",
                ),
                range(2),
            )
        )
    assert provider.analyze_calls == 1
    assert calls == 1
    assert {result["stage"] for result in results} == {"single", "cache"}


def test_concurrent_force_requests_share_one_fresh_generation(app, tmp_path):
    actor = create_admin(app)
    photo_id = _scan(app, tmp_path)[0]
    _setting(app, "analysis.ai_mode", "eligible")
    _setting(app, "analysis.prefilter_enabled", False)
    service = app.extensions["inktime_analysis_service"]
    initial = CountingProvider(valid_result(memory_score=31))
    service.analyze_photo(
        photo_id=photo_id, job_id=None, provider=initial, strategy="high_quality", high_model="force"
    )
    force = SlowCountingProvider(valid_result(memory_score=77))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _ignored: service.analyze_photo(
                    photo_id=photo_id,
                    job_id=None,
                    provider=force,
                    strategy="high_quality",
                    high_model="force",
                    force_ai=True,
                    force_actor=actor,
                    force_recompute=True,
                ),
                range(2),
            )
        )
    assert initial.analyze_calls == 1
    assert force.analyze_calls == 1
    assert {result["analysis"]["memory_score"] for result in results} == {77}


def test_cache_wait_deadline_covers_provider_and_one_json_repair(app, tmp_path, monkeypatch):
    photo_id = _scan(app, tmp_path)[0]
    photos = app.extensions["inktime_photo_repository"]
    photo = photos.get_with_path(photo_id)
    assert photo is not None
    service = app.extensions["inktime_analysis_service"]
    provider = CountingProvider()
    leases: list[int] = []
    acquisitions = iter([False])
    cached = {
        "result": valid_result(),
        "raw_json": json.dumps(valid_result(), ensure_ascii=False),
        "created_at": "fresh-owner-result",
    }
    cache_reads = 0

    def acquire(_key, _owner, *, lease_seconds=480):
        leases.append(lease_seconds)
        return next(acquisitions, False)

    def get_cache(**_kwargs):
        nonlocal cache_reads
        cache_reads += 1
        # Full-analysis cache lookup accepts a current-schema row, a newly
        # written v2 row carrying the canonical fingerprint, and the
        # historical v2 fingerprint.
        return None if cache_reads <= 3 else cached

    clock = iter([0.0, 121.0])
    monkeypatch.setattr(photos, "acquire_ai_cache_reservation", acquire)
    monkeypatch.setattr(photos, "get_ai_cache", get_cache)
    monkeypatch.setattr(analysis_module.time, "monotonic", lambda: next(clock, 121.0))
    monkeypatch.setattr(analysis_module.time, "sleep", lambda _seconds: None)

    result = service._model_call(
        provider=provider,
        image_factory=lambda: (_ for _ in ()).throw(AssertionError("waiter must reuse owner cache")),
        model="wait-deadline",
        detail="high",
        stage="single_high",
        job_id=None,
        photo_id=photo_id,
        content_sha256=str(photo["sha256"]),
        schema_kind="full",
        caption_controls=None,
        repair_policy={
            "enabled": True,
            "model": "wait-deadline-repair",
            "max_tokens": 1200,
            "max_attempts": 1,
            "text_only": True,
        },
        prompt_version="test",
        vision_input={"mode": "test"},
    )
    assert result[3] is True
    assert provider.analyze_calls == 0
    assert leases == [252]


def test_full_json_allows_missing_nonessential_fields_and_travel_bonus_is_independent(app, tmp_path):
    photo_id = _scan(app, tmp_path)[0]
    _setting(app, "analysis.ai_mode", "eligible")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET gps_lat=22.6273,gps_lon=120.3014 WHERE id=?", (photo_id,))
    result = valid_result(schema_version=2)
    provider = CountingProvider(result)
    analyzed = app.extensions["inktime_analysis_service"].analyze_photo(
        photo_id=photo_id, job_id=None, provider=provider, strategy="high_quality", high_model="travel"
    )
    assert analyzed["analysis"]["memory_score"] == result["memory_score"]
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT memory_score,travel_bonus,base_ranking_score,final_ranking_score FROM photo_analysis WHERE photo_id=?",
            (photo_id,),
        ).fetchone()
    assert row["memory_score"] == result["memory_score"]
    assert row["travel_bonus"] > 0
    assert row["final_ranking_score"] == row["base_ranking_score"] + row["travel_bonus"]


def test_full_library_confirmation_and_queue_count_only_active_eligible_photos(client, app):
    create_admin(app)
    login(client)
    _enable_fake_usable_provider(app)
    now = datetime.now(timezone.utc).isoformat()
    library_id = str(uuid4())
    eligible_ids = [str(uuid4()) for _ in range(3)]
    excluded_id = str(uuid4())
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "INSERT INTO libraries(id,name,root_path,created_at,updated_at) VALUES (?,?,?,?,?)",
            (library_id, "完整選片", "/photos", now, now),
        )
        connection.executemany(
            """
            INSERT INTO photos(id,library_id,relative_path,status,eligible,lifecycle_status,
                               local_candidate_score,created_at,updated_at)
            VALUES (?,?,?,'preprocessed',?,'active',?,?,?)
            """,
            [
                (photo_id, library_id, f"{index}.jpg", 1, float(10 - index), now, now)
                for index, photo_id in enumerate(eligible_ids)
            ]
            + [(excluded_id, library_id, "excluded.jpg", 0, 99.0, now, now)],
        )
    _setting(app, "analysis.ai_mode", "full_library")
    _setting(app, "analysis.ai_daily_photo_limit", 2)
    headers = {"X-CSRF-Token": csrf(client)}
    confirmation = client.post("/api/v1/photos/ai/run", json={}, headers=headers)
    assert confirmation.status_code == 409
    assert confirmation.json["photos"] == 2
    assert confirmation.json["eligible_total"] == 3

    queued = client.post("/api/v1/photos/ai/run", json={"confirm": True, "batch_by": "year"}, headers=headers)
    assert queued.status_code == 201
    assert queued.json["queued"] == 2
    with app.extensions["inktime_database"].session() as connection:
        photo_ids = {
            str(row["photo_id"])
            for row in connection.execute("SELECT photo_id FROM job_items WHERE photo_id IS NOT NULL")
        }
        job_settings = [
            json.loads(str(row["settings_json"]))
            for row in connection.execute("SELECT settings_json FROM jobs WHERE kind='analysis'")
        ]
    assert photo_ids <= set(eligible_ids)
    assert excluded_id not in photo_ids
    assert job_settings and all(settings["force_ai"] is True for settings in job_settings)


def test_manual_vision_job_marks_every_selected_item_for_provider_call(client, app):
    create_admin(app)
    login(client)
    _enable_fake_usable_provider(app)
    _setting(app, "analysis.execution_mode", "automatic_ai")
    photo_ids = add_photos(app, 3)

    response = client.post(
        "/api/v1/jobs",
        json={"name": "手動完整 Vision", "strategy": "single", "photo_ids": photo_ids},
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 201, response.json
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT settings_json FROM jobs WHERE id=?", (response.json["id"],)
        ).fetchone()
    assert json.loads(str(row["settings_json"])) == {
        "force_ai": True,
        "source": "manual-job",
    }


def test_full_library_group_idempotency_is_bounded_replayable_and_conflict_safe(client, app, monkeypatch):
    create_admin(app)
    login(client)
    _enable_fake_usable_provider(app)
    now = datetime.now(timezone.utc).isoformat()
    library_id = str(uuid4())
    initial_ids = [str(uuid4()), str(uuid4())]
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "INSERT INTO libraries(id,name,root_path,created_at,updated_at) VALUES (?,?,?,?,?)",
            (library_id, "分組冪等測試", "/photos", now, now),
        )
        connection.executemany(
            """
            INSERT INTO photos(id,library_id,relative_path,status,eligible,lifecycle_status,
                               local_candidate_score,created_at,updated_at)
            VALUES (?,?,?,'preprocessed',1,'active',?,?,?)
            """,
            [
                (initial_ids[0], library_id, "group-a/first.jpg", 10.0, now, now),
                (initial_ids[1], library_id, "group-b/second.jpg", 9.0, now, now),
            ],
        )
    _setting(app, "analysis.ai_mode", "full_library")
    _setting(app, "analysis.ai_daily_photo_limit", 10)
    client_key = "K" * 128
    headers = {
        "X-CSRF-Token": csrf(client),
        "Idempotency-Key": client_key,
    }
    reservation_events = []
    job_repository = app.extensions["inktime_job_repository"]
    photo_repository = app.extensions["inktime_photo_repository"]
    original_reserve = job_repository.reserve_idempotent_request
    original_enumerate = photo_repository.eligible_photo_batches

    def tracked_reserve(*args, **kwargs):
        reservation_events.append("reserve")
        return original_reserve(*args, **kwargs)

    def tracked_enumerate(*args, **kwargs):
        reservation_events.append("enumerate")
        return original_enumerate(*args, **kwargs)

    monkeypatch.setattr(job_repository, "reserve_idempotent_request", tracked_reserve)
    monkeypatch.setattr(photo_repository, "eligible_photo_batches", tracked_enumerate)

    first = client.post(
        "/api/v1/photos/ai/run",
        json={"confirm": True, "batch_by": "folder"},
        headers=headers,
    )
    assert first.status_code == 201
    assert reservation_events[:2] == ["reserve", "enumerate"]
    first_job_ids = [job["id"] for job in first.json["jobs"]]
    assert len(first_job_ids) == 2
    with app.extensions["inktime_database"].session() as connection:
        first_rows = connection.execute(
            "SELECT id,dedupe_key FROM jobs WHERE kind='analysis' ORDER BY created_at,id"
        ).fetchall()
    assert [str(row["id"]) for row in first_rows] == first_job_ids
    assert len({str(row["dedupe_key"]) for row in first_rows}) == 2

    replay = client.post(
        "/api/v1/photos/ai/run",
        json={"confirm": True, "batch_by": "folder"},
        headers={**headers, "Idempotency-Key": f"  {client_key}  "},
    )
    assert replay.status_code == 201
    assert [job["id"] for job in replay.json["jobs"]] == first_job_ids

    long_key_replay = client.post(
        "/api/v1/photos/ai/run",
        json={"confirm": True, "batch_by": "folder"},
        headers={**headers, "Idempotency-Key": f"{client_key}tail"},
    )
    assert long_key_replay.status_code == 201
    assert [job["id"] for job in long_key_replay.json["jobs"]] == first_job_ids

    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            INSERT INTO photos(id,library_id,relative_path,status,eligible,lifecycle_status,
                               local_candidate_score,created_at,updated_at)
            VALUES (?,?,?,'preprocessed',1,'active',?,?,?)
            """,
            (str(uuid4()), library_id, "group-a/new.jpg", 8.0, now, now),
        )
        before_conflict_count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    same_request_after_library_change = client.post(
        "/api/v1/photos/ai/run",
        json={"confirm": True, "batch_by": "folder"},
        headers=headers,
    )
    assert same_request_after_library_change.status_code == 201
    assert [job["id"] for job in same_request_after_library_change.json["jobs"]] == first_job_ids
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before_conflict_count

    changed_batch = client.post(
        "/api/v1/photos/ai/run",
        json={"confirm": True, "batch_by": "year"},
        headers=headers,
    )
    assert changed_batch.status_code == 409
    assert changed_batch.json["error_code"] == "IDEMPOTENCY_CONFLICT"
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before_conflict_count

    _setting(app, "analysis.strategy", "local")
    changed_plan = client.post(
        "/api/v1/photos/ai/run",
        json={"confirm": True, "batch_by": "folder"},
        headers=headers,
    )
    assert changed_plan.status_code == 409
    assert changed_plan.json["error_code"] == "IDEMPOTENCY_CONFLICT"
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before_conflict_count


def test_full_library_concurrent_request_has_single_enumeration_owner(app, tmp_path, monkeypatch):
    create_admin(app)
    _enable_fake_usable_provider(app)
    now = datetime.now(timezone.utc).isoformat()
    library_id = str(uuid4())
    photo_id = str(uuid4())
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "INSERT INTO libraries(id,name,root_path,created_at,updated_at) VALUES (?,?,?,?,?)",
            (library_id, "並行預約", "/photos", now, now),
        )
        connection.execute(
            """
            INSERT INTO photos(id,library_id,relative_path,status,eligible,lifecycle_status,
                               local_candidate_score,created_at,updated_at)
            VALUES (?,?,?,'preprocessed',1,'active',?,?,?)
            """,
            (photo_id, library_id, "single.jpg", 10.0, now, now),
        )
    _setting(app, "analysis.ai_mode", "full_library")
    _setting(app, "analysis.ai_daily_photo_limit", 10)
    monkeypatch.setattr(photos_api, "IDEMPOTENCY_RESERVATION_LEASE_SECONDS", 1)
    monkeypatch.setattr(photos_api, "IDEMPOTENCY_RESERVATION_HEARTBEAT_SECONDS", 0.05)
    photo_repository = app.extensions["inktime_photo_repository"]
    original_enumerate = photo_repository.eligible_photo_batches
    enumeration_started = threading.Event()
    release_enumeration = threading.Event()
    enumeration_calls = 0
    calls_lock = threading.Lock()

    def blocked_enumerate(*args, **kwargs):
        nonlocal enumeration_calls
        with calls_lock:
            enumeration_calls += 1
        enumeration_started.set()
        assert release_enumeration.wait(5)
        return original_enumerate(*args, **kwargs)

    monkeypatch.setattr(photo_repository, "eligible_photo_batches", blocked_enumerate)

    def submit_request():
        with app.test_client() as concurrent_client:
            login(concurrent_client)
            return concurrent_client.post(
                "/api/v1/photos/ai/run",
                json={"confirm": True, "batch_by": "folder"},
                headers={
                    "X-CSRF-Token": csrf(concurrent_client),
                    "Idempotency-Key": "concurrent-full-library-key",
                },
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(submit_request)
        assert enumeration_started.wait(5)
        time.sleep(1.5)
        second_response = pool.submit(submit_request).result(timeout=5)
        assert second_response.status_code == 409
        assert second_response.json["error_code"] == "IDEMPOTENCY_IN_PROGRESS"
        release_enumeration.set()
        first_response = first_future.result(timeout=10)

    assert first_response.status_code == 201
    assert enumeration_calls == 1


def test_full_library_in_progress_ledger_resumes_partial_group_creation(client, app, monkeypatch):
    create_admin(app)
    login(client)
    _enable_fake_usable_provider(app)
    now = datetime.now(timezone.utc).isoformat()
    library_id = str(uuid4())
    photo_ids = [str(uuid4()), str(uuid4())]
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "INSERT INTO libraries(id,name,root_path,created_at,updated_at) VALUES (?,?,?,?,?)",
            (library_id, "部分恢復", "/photos", now, now),
        )
        connection.executemany(
            """
            INSERT INTO photos(id,library_id,relative_path,status,eligible,lifecycle_status,
                               local_candidate_score,created_at,updated_at)
            VALUES (?,?,?,'preprocessed',1,'active',?,?,?)
            """,
            [
                (photo_ids[0], library_id, "group-a/first.jpg", 10.0, now, now),
                (photo_ids[1], library_id, "group-b/second.jpg", 9.0, now, now),
            ],
        )
    _setting(app, "analysis.ai_mode", "full_library")
    _setting(app, "analysis.ai_daily_photo_limit", 10)
    headers = {"X-CSRF-Token": csrf(client), "Idempotency-Key": "partial-resume-key"}
    original_queue_ai = photos_api._queue_ai
    calls = 0

    def fail_after_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("simulated partial group failure")
        return original_queue_ai(*args, **kwargs)

    monkeypatch.setattr(photos_api, "_queue_ai", fail_after_first)
    interrupted = client.post(
        "/api/v1/photos/ai/run", json={"confirm": True, "batch_by": "folder"}, headers=headers
    )
    assert interrupted.status_code == 409
    monkeypatch.setattr(photos_api, "_queue_ai", original_queue_ai)
    resumed = client.post(
        "/api/v1/photos/ai/run", json={"confirm": True, "batch_by": "folder"}, headers=headers
    )
    assert resumed.status_code == 201
    assert len(resumed.json["jobs"]) == 2
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE kind='analysis'").fetchone()[0] == 2
        ledger = connection.execute(
            "SELECT status FROM idempotency_requests WHERE scope_key LIKE 'idempotency:ai-mode-run/full-library-request:%'"
        ).fetchone()
    assert ledger["status"] == "completed"


def test_full_library_without_provider_fails_before_enumeration_or_reservation(
    client, app, monkeypatch
):
    create_admin(app)
    login(client)
    _setting(app, "analysis.ai_mode", "full_library")
    photo_repository = app.extensions["inktime_photo_repository"]

    def unexpected_enumeration(*_args, **_kwargs):
        raise AssertionError("Provider gate must run before full-library enumeration")

    monkeypatch.setattr(photo_repository, "eligible_photo_batches", unexpected_enumeration)
    unconfirmed = client.post(
        "/api/v1/photos/ai/run",
        json={},
        headers={"X-CSRF-Token": csrf(client)},
    )
    response = client.post(
        "/api/v1/photos/ai/run",
        json={"confirm": True, "batch_by": "folder"},
        headers={
            "X-CSRF-Token": csrf(client),
            "Idempotency-Key": "no-provider-full-library",
        },
    )

    assert unconfirmed.status_code == 409
    assert unconfirmed.json["error_code"] == "VLM-008"
    assert response.status_code == 409
    assert {key: response.json[key] for key in ("error_code", "message")} == {
        "error_code": "VLM-008",
        "message": "目前沒有已啟用且設定完整的 Vision Provider",
    }
    assert "模型設定" in response.json["user_error"]["title"]
    with app.extensions["inktime_database"].session() as connection:
        assert int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]) == 0
        assert int(connection.execute("SELECT COUNT(*) FROM idempotency_requests").fetchone()[0]) == 0


def test_concurrent_full_library_without_provider_is_deterministic_and_side_effect_free(app):
    create_admin(app)
    _setting(app, "analysis.ai_mode", "full_library")

    def submit_request():
        with app.test_client() as concurrent_client:
            login(concurrent_client)
            return concurrent_client.post(
                "/api/v1/photos/ai/run",
                json={"confirm": True, "batch_by": "folder"},
                headers={
                    "X-CSRF-Token": csrf(concurrent_client),
                    "Idempotency-Key": "concurrent-no-provider",
                },
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _index: submit_request(), range(2)))

    assert {response.status_code for response in responses} == {409}
    assert {response.json["error_code"] for response in responses} == {"VLM-008"}
    with app.extensions["inktime_database"].session() as connection:
        assert int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]) == 0
        assert int(connection.execute("SELECT COUNT(*) FROM idempotency_requests").fetchone()[0]) == 0


def test_thumbnail_cleanup_only_queries_hashes_visible_in_cache(app, tmp_path):
    photo_ids = _scan(app, tmp_path, duplicate=True)
    repository = app.extensions["inktime_photo_repository"]
    first = repository.get_with_path(photo_ids[0])
    second = repository.get_with_path(photo_ids[1])
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET lifecycle_status='missing' WHERE id=?", (photo_ids[1],))

    assert repository.active_hashes_for([str(first["sha256"]), str(second["sha256"]), "not-a-sha"]) == {
        str(first["sha256"])
    }
