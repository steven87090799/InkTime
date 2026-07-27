from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier


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
        assert repository.get_ai_cache(
            content_sha256="a" * 64,
            provider="provider-id",
            model_name="vision",
            prompt_version="prompt-v4",
            schema_version=2,
            schema_kind="full",
            vision_request_fingerprint=fingerprint,
        ) is not None
