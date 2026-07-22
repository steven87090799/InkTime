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
