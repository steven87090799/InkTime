from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import multiprocessing
import os
from pathlib import Path
import threading
import time

from PIL import Image

from inktime.app.domain.photos.thumbnails import ThumbnailCache


def _thumbnail_process(root: str, source: str, digest: str, output) -> None:
    cache = ThumbnailCache(Path(root))
    output.put(str(cache.get_or_create(Path(source), digest, 512)))


def _hold_thumbnail_lock_process(root: str, key: str, active, maximum, guard) -> None:
    cache = ThumbnailCache(Path(root))
    with cache._generation_lock(key):
        with guard:
            active.value += 1
            maximum.value = max(maximum.value, active.value)
        time.sleep(0.05)
        with guard:
            active.value -= 1


def _source(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "source.jpg"
    Image.new("RGB", (900, 600), (20, 80, 140)).save(path, "JPEG")
    return path, sha256(path.read_bytes()).hexdigest()


def test_same_key_is_mutually_exclusive_across_threads(tmp_path):
    cache = ThumbnailCache(tmp_path / "cache")
    key = "a" * 64 + "-512"
    active = 0
    maximum = 0
    guard = threading.Lock()

    def hold(_index: int) -> None:
        nonlocal active, maximum
        with cache._generation_lock(key):
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.01)
            with guard:
                active -= 1

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(hold, range(20)))
    assert maximum == 1


def test_same_thumbnail_is_safe_across_processes_and_no_per_photo_lock_is_created(tmp_path):
    source, digest = _source(tmp_path)
    cache_root = tmp_path / "cache"
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [
        context.Process(
            target=_thumbnail_process,
            args=(str(cache_root), str(source), digest, output),
        )
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    results = [output.get(timeout=2) for _ in processes]
    assert len(set(results)) == 1
    assert Path(results[0]).is_file()
    assert not list(cache_root.glob(f".{digest}-512.lock"))
    assert len(list((cache_root / ".locks").glob("shard-*.lock"))) <= 1


def test_same_key_is_mutually_exclusive_across_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    active = context.Value("i", 0)
    maximum = context.Value("i", 0)
    guard = context.Lock()
    root = tmp_path / "cache"
    processes = [
        context.Process(
            target=_hold_thumbnail_lock_process,
            args=(str(root), "c" * 64 + "-512", active, maximum, guard),
        )
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert maximum.value == 1


def test_different_shards_can_run_in_parallel(tmp_path):
    cache = ThumbnailCache(tmp_path / "cache")
    first = "a" * 64 + "-512"
    first_shard = int(sha256(first.encode("ascii")).hexdigest()[:8], 16) % cache.LOCK_SHARDS
    second = next(
        f"{index:064x}-512"
        for index in range(1, 10_000)
        if int(sha256(f"{index:064x}-512".encode("ascii")).hexdigest()[:8], 16) % cache.LOCK_SHARDS
        != first_shard
    )
    barrier = threading.Barrier(2)
    entered = []

    def hold(key: str) -> None:
        with cache._generation_lock(key):
            entered.append(key)
            barrier.wait(timeout=2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(hold, (first, second)))
    assert len(entered) == 2


def test_cleanup_skips_a_thumbnail_held_by_an_active_use_lease(tmp_path):
    source, digest = _source(tmp_path)
    cache = ThumbnailCache(tmp_path / "cache")
    thumbnail = cache.get_or_create(source, digest, 512)
    acquired = threading.Event()
    release = threading.Event()

    def provider_read() -> None:
        with cache.acquire_for_use(source, digest, 512) as path:
            assert path.is_file()
            acquired.set()
            assert release.wait(2)
            assert path.read_bytes()

    thread = threading.Thread(target=provider_read)
    thread.start()
    assert acquired.wait(2)
    skipped = cache.cleanup(max_bytes=0, retention_days=0, active_hashes=set())
    assert skipped["files"] == 0
    assert thumbnail.exists()
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    removed = cache.cleanup(max_bytes=0, retention_days=0, active_hashes=set())
    assert removed["files"] == 1
    assert not thumbnail.exists()


def test_cleanup_reuses_an_explicit_empty_inventory(tmp_path, monkeypatch):
    cache = ThumbnailCache(tmp_path / "cache")

    def unexpected_inventory():
        raise AssertionError("cleanup must not rescan an explicitly supplied inventory")

    monkeypatch.setattr(cache, "inventory", unexpected_inventory)
    assert cache.cleanup(
        max_bytes=0,
        retention_days=0,
        active_hashes=set(),
        inventory=[],
    ) == {"files": 0, "bytes": 0}


def test_retained_preview_survives_expiry_and_capacity_until_record_removed(tmp_path):
    source, digest = _source(tmp_path)
    cache = ThumbnailCache(tmp_path / "cache")
    thumbnail = cache.get_or_create(source, digest, 512)
    large = cache.get_or_create(source, digest, 1600)
    os.utime(thumbnail, (1, 1))
    source.unlink()
    result = cache.cleanup(max_bytes=0, retention_days=1, active_hashes={digest})
    assert result["files"] == 1
    assert thumbnail.exists() and not large.exists()
    with cache.acquire_existing(digest, 1600) as retained:
        assert retained == thumbnail
    cache.cleanup(max_bytes=0, retention_days=1, active_hashes=set())
    with cache.acquire_existing(digest, 1600) as retained:
        assert retained is None
