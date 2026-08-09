from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import queue
import time

from PIL import Image
import pytest

from inktime.app.domain.rendering.release import (
    AtomicReleasePublisher,
    DEVICE_TEST_INDEX_MIGRATION_DIRECTORY,
    DEVICE_TEST_INDEX_MIGRATION_VERSION,
    ReleaseMetadataLockTimeout,
    release_metadata_guard,
)


def _publish_preencoded(
    root: str,
    payload_path: str,
    preview_path: str,
    idempotency_key: str,
    start,
    results,
) -> None:
    start.wait(10)
    publisher = AtomicReleasePublisher(Path(root))
    try:
        manifest = publisher.publish_preencoded(
            source_photo_id="multiprocess-test",
            payload_path=Path(payload_path),
            preview_path=Path(preview_path),
            profile_key="safe_4c",
            dither="none",
            color_distance="rgb",
            dither_strength=0,
            linear_light=False,
            palette=[],
            palette_version="test",
            metadata={
                "idempotency_key": idempotency_key,
                "transport": "custom",
                "stock_direct": False,
            },
        )
    except Exception as exc:  # pragma: no cover - asserted through child result.
        results.put(("error", type(exc).__name__))
    else:
        results.put(("published", manifest["release_id"]))


def _repair_index(root: str, idempotency_key: str, start, results) -> None:
    start.wait(10)
    manifest = AtomicReleasePublisher(Path(root)).find_device_test_by_idempotency(idempotency_key)
    results.put(("repair", None if manifest is None else manifest["release_id"]))


def _discard_release(root: str, release_id: str, idempotency_key: str, start, results) -> None:
    start.wait(10)
    AtomicReleasePublisher(Path(root)).discard_unassigned_device_test(release_id, idempotency_key)
    results.put(("discarded", release_id))


def _hold_metadata_lock(root: str, ready, hold_seconds: float) -> None:
    with release_metadata_guard(Path(root), timeout=2):
        ready.set()
        time.sleep(hold_seconds)


def _fixture_files(tmp_path: Path) -> tuple[Path, Path]:
    payload = tmp_path / "payload.bin"
    preview = tmp_path / "preview.png"
    payload.write_bytes(bytes(96_000))
    Image.new("RGB", (480, 800), "white").save(preview)
    return payload, preview


def _child_results(results, count: int) -> list[tuple[str, str | None]]:
    values = []
    for _index in range(count):
        try:
            values.append(results.get(timeout=10))
        except queue.Empty:
            pytest.fail("multiprocess regression did not return a bounded result")
    return values


def test_multiprocess_same_idempotency_key_has_one_live_index(tmp_path):
    context = multiprocessing.get_context("spawn")
    root = tmp_path / "releases"
    payload, preview = _fixture_files(tmp_path)
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_publish_preencoded,
            args=(str(root), str(payload), str(preview), "shared-key", start, results),
        )
        for _index in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0

    values = _child_results(results, 2)
    published = [value for status, value in values if status == "published"]
    assert len(published) == 1
    assert [status for status, _value in values].count("error") == 1
    manifest = AtomicReleasePublisher(root).find_device_test_by_idempotency("shared-key")
    assert manifest is not None and manifest["release_id"] == published[0]
    live = [path.name for path in root.iterdir() if (path / "manifest.json").is_file()]
    assert live == published


def test_multiprocess_stale_repair_cannot_delete_new_publish_index(tmp_path):
    context = multiprocessing.get_context("spawn")
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    key = "repair-vs-publish"
    index = publisher._device_test_index_path(key)
    index.parent.mkdir()
    index.write_text("{", encoding="utf-8")
    state_root = root / DEVICE_TEST_INDEX_MIGRATION_DIRECTORY
    state_root.mkdir()
    (state_root / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "migration_version": DEVICE_TEST_INDEX_MIGRATION_VERSION,
                "complete": True,
                "completed_at": "2026-08-09T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    payload, preview = _fixture_files(tmp_path)
    start = context.Event()
    results = context.Queue()
    repair = context.Process(target=_repair_index, args=(str(root), key, start, results))
    publish = context.Process(
        target=_publish_preencoded,
        args=(str(root), str(payload), str(preview), key, start, results),
    )
    repair.start()
    publish.start()
    start.set()
    repair.join(15)
    publish.join(15)
    assert repair.exitcode == publish.exitcode == 0
    values = _child_results(results, 2)
    published = next(value for status, value in values if status == "published")
    manifest = AtomicReleasePublisher(root).find_device_test_by_idempotency(key)
    assert manifest is not None and manifest["release_id"] == published


def test_multiprocess_migration_cannot_overwrite_new_publish_index(tmp_path):
    context = multiprocessing.get_context("spawn")
    root = tmp_path / "releases"
    AtomicReleasePublisher(root)
    payload, preview = _fixture_files(tmp_path)
    start = context.Event()
    results = context.Queue()
    migrate = context.Process(
        target=_repair_index,
        args=(str(root), "migration-vs-publish", start, results),
    )
    publish = context.Process(
        target=_publish_preencoded,
        args=(
            str(root),
            str(payload),
            str(preview),
            "migration-vs-publish",
            start,
            results,
        ),
    )
    migrate.start()
    publish.start()
    start.set()
    migrate.join(15)
    publish.join(15)
    assert migrate.exitcode == publish.exitcode == 0
    values = _child_results(results, 2)
    published = next(value for status, value in values if status == "published")
    manifest = AtomicReleasePublisher(root).find_device_test_by_idempotency("migration-vs-publish")
    assert manifest is not None and manifest["release_id"] == published


def test_multiprocess_discard_and_new_publish_never_delete_replacement(tmp_path):
    context = multiprocessing.get_context("spawn")
    root = tmp_path / "releases"
    payload, preview = _fixture_files(tmp_path)
    publisher = AtomicReleasePublisher(root)
    initial_results = context.Queue()
    immediate = context.Event()
    immediate.set()
    _publish_preencoded(str(root), str(payload), str(preview), "cleanup-race", immediate, initial_results)
    initial = _child_results(initial_results, 1)[0][1]
    assert initial is not None
    start = context.Event()
    results = context.Queue()
    discard = context.Process(
        target=_discard_release,
        args=(str(root), initial, "cleanup-race", start, results),
    )
    publish = context.Process(
        target=_publish_preencoded,
        args=(str(root), str(payload), str(preview), "cleanup-race", start, results),
    )
    discard.start()
    publish.start()
    start.set()
    discard.join(15)
    publish.join(15)
    assert discard.exitcode == publish.exitcode == 0
    values = _child_results(results, 2)
    published = [value for status, value in values if status == "published"]
    if published:
        manifest = publisher.find_device_test_by_idempotency("cleanup-race")
        assert manifest is not None and manifest["release_id"] == published[0]
        assert (root / published[0]).is_dir()
    else:
        assert [status for status, _value in values].count("error") == 1


def test_metadata_lock_is_released_when_holder_process_is_terminated(tmp_path):
    context = multiprocessing.get_context("spawn")
    root = tmp_path / "releases"
    root.mkdir()
    ready = context.Event()
    holder = context.Process(target=_hold_metadata_lock, args=(str(root), ready, 30))
    holder.start()
    assert ready.wait(10)
    holder.terminate()
    holder.join(10)
    assert holder.exitcode is not None
    with release_metadata_guard(root, timeout=1):
        assert (root / ".release-metadata.lock").is_file()


def test_metadata_lock_timeout_is_bounded_and_reentrant(tmp_path):
    context = multiprocessing.get_context("spawn")
    root = tmp_path / "releases"
    root.mkdir()
    ready = context.Event()
    holder = context.Process(target=_hold_metadata_lock, args=(str(root), ready, 1))
    holder.start()
    assert ready.wait(10)
    started = time.monotonic()
    with pytest.raises(ReleaseMetadataLockTimeout):
        with release_metadata_guard(root, timeout=0.1):
            pytest.fail("contended guard must not be entered")
    assert time.monotonic() - started < 0.75
    holder.join(10)
    assert holder.exitcode == 0
    with release_metadata_guard(root, timeout=1):
        with release_metadata_guard(root, timeout=1):
            assert (root / ".release-metadata.lock").is_file()


def test_metadata_lock_rejects_symlink_and_hardlink_aliases(tmp_path):
    outside = tmp_path / "outside-lock"
    outside.write_text("preserve", encoding="utf-8")
    for kind in ("symlink", "hardlink"):
        root = tmp_path / kind
        root.mkdir()
        lock = root / ".release-metadata.lock"
        if kind == "symlink":
            lock.symlink_to(outside)
        else:
            lock.hardlink_to(outside)
        with pytest.raises((OSError, ValueError)):
            with release_metadata_guard(root, timeout=0.1):
                pytest.fail("unsafe lock alias must not be entered")
        assert outside.read_text(encoding="utf-8") == "preserve"
