from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier


def test_fallback_cache_tracks_selected_channel_behavior_revision(app, tmp_path, monkeypatch):
    from contextlib import nullcontext
    from PIL import Image
    from inktime.app.providers.router import FailoverVisionProvider, ProviderChannel
    from tests.unit.test_batch_analysis_lifecycle import _prepare_photos
    from tests.unit.test_scoring import LabProvider

    photo_id = _prepare_photos(app, tmp_path, count=1)[0]
    service = app.extensions["inktime_analysis_service"]
    app.extensions["inktime_settings_repository"].update(
        "analysis.execution_mode", "local_with_manual_ai", changed_by="test", source_ip="test"
    )
    monkeypatch.setattr(service, "process_boundary", None)
    with service.photos.database.session() as connection:
        photo = connection.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    primary = LabProvider()
    primary.name = "primary"
    fallback = LabProvider()
    fallback.name = "fallback"
    channel = ProviderChannel(fallback, priority=2, analysis_revision="old-endpoint")
    router = FailoverVisionProvider([ProviderChannel(primary, priority=1), channel])
    calls = []
    original = service._perform_uncached_model_call

    def external_call(**kwargs):
        name = kwargs["selected_channel"].provider.name
        if name == "primary":
            raise ValueError("VLM-005 pre-dispatch primary failure")
        calls.append(name)
        return original(**kwargs)

    monkeypatch.setattr(service, "_perform_uncached_model_call", external_call)
    image = tmp_path / "synthetic.jpg"
    Image.new("RGB", (64, 64), "orange").save(image)
    parameters = dict(provider=router, image_factory=lambda: nullcontext(image),
                      model="vision", detail="high", stage="single", job_id=None,
                      photo_id=photo_id, content_sha256=photo["sha256"], schema_kind="full",
                      caption_controls=None, prompt_version="test-v5", vision_input={"max_side": 1024},
                      provider_prompt_contract_sha256="primary-contract")
    first = service._model_call(**parameters)
    cached = service._model_call(**parameters)
    assert cached[3] is True
    assert first[6] == cached[6]
    channel.analysis_revision = "new-endpoint"
    changed = service._model_call(**parameters)
    assert changed[3] is False
    assert changed[6] != first[6]
    assert calls == ["fallback", "fallback"]


def test_ai_cache_reservation_has_one_owner_and_failed_lease_recovers(app):
    repository = app.extensions["inktime_photo_repository"]
    barrier = Barrier(2)

    def acquire(owner: str) -> bool:
        barrier.wait()
        return repository.acquire_ai_cache_reservation("same-cache-key", owner, lease_seconds=30)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(acquire, ("owner-a", "owner-b")))
    assert sorted(results) == [False, True]
    owner = "owner-a" if results[0] else "owner-b"
    repository.finish_ai_cache_reservation("same-cache-key", owner, error="provider failed")
    assert repository.acquire_ai_cache_reservation("same-cache-key", "takeover") is True


def test_vision_input_variants_keep_independent_cache_rows(app):
    repository = app.extensions["inktime_photo_repository"]
    common = {
        "content_sha256": "a" * 64,
        "provider": "provider-id",
        "model_name": "vision",
        "prompt_version": "prompt-v4",
        "schema_version": 2,
        "schema_kind": "full",
        "result": {"schema_version": 2},
        "raw_json": "{}",
        "input_tokens": 1,
        "output_tokens": 1,
        "cached_tokens": 0,
        "estimated_cost": 0,
        "latency_ms": 1,
    }
    for fingerprint in ("1" * 64, "2" * 64, "3" * 64):
        repository.put_ai_cache(
            **common,
            vision_request_fingerprint=fingerprint,
            vision_input_spec_json='{"max_side":1024}',
        )
    for fingerprint in ("1" * 64, "2" * 64, "3" * 64):
        assert (
            repository.get_ai_cache(
                content_sha256="a" * 64,
                provider="provider-id",
                model_name="vision",
                prompt_version="prompt-v4",
                schema_version=2,
                schema_kind="full",
                vision_request_fingerprint=fingerprint,
            )
            is not None
        )
