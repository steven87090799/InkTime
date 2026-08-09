from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil

from PIL import Image
import pytest

from inktime.app.domain.rendering.fonts import FontCoverageError
from inktime.app.domain.rendering.palette import encode_image
from inktime.app.domain.rendering.release import (
    AtomicReleasePublisher,
    DEVICE_TEST_INDEX_DIRECTORY,
    DEVICE_TEST_INDEX_MIGRATION_VERSION,
    DEVICE_TEST_INDEX_MIGRATION_DIRECTORY,
    DEVICE_TEST_INDEX_QUARANTINE_DIRECTORY,
    DeviceTestReleaseStore,
    STOCK_DIRECT_TEST_DEFERRED_DIRECTORY,
    STOCK_DIRECT_TEST_DIRECTORY,
    pack_four_color_2bpp,
)


def test_four_color_480x800_is_96000_bytes():
    image = Image.new("RGB", (480, 800), "white")
    payload = pack_four_color_2bpp(image)
    assert len(payload) == 96_000
    assert set(payload) == {0b01010101}


def test_atomic_release_manifest_and_rollback(tmp_path):
    publisher = AtomicReleasePublisher(tmp_path / "releases")
    first = publisher.publish([("photo-1", Image.new("RGB", (480, 800), "red"))])
    release_dir = tmp_path / "releases" / first["release_id"]
    payload = (release_dir / "photo_1.bin").read_bytes()
    manifest = json.loads((release_dir / "manifest.json").read_text())
    assert manifest["pixel_format"] == "2bpp"
    assert manifest["files"][0]["size"] == 96_000
    assert manifest["files"][0]["sha256"] == sha256(payload).hexdigest()
    assert (tmp_path / "releases" / "latest").read_text() == first["release_id"]

    second = publisher.publish(
        [("photo-2", Image.new("RGB", (480, 800), "black"))],
        orientation="landscape",
    )
    assert second["orientation"] == "landscape"
    publisher.rollback(first["release_id"])
    assert (tmp_path / "releases" / "latest").read_text() == first["release_id"]
    assert second["release_id"] != first["release_id"]


def test_gooddisplay_release_records_effective_vendor_palette(tmp_path):
    publisher = AtomicReleasePublisher(tmp_path / "releases")
    manifest = publisher.publish(
        [("photo-1", Image.new("RGB", (480, 800), (60, 120, 210)))],
        profile_key="gdep073e01_6c",
        dither="gooddisplay",
        color_distance="oklab",
        dither_strength=0.4,
    )

    assert manifest["dither"] == "gooddisplay"
    assert manifest["dither_strength"] == 1.0
    assert manifest["color_distance"] == "rgb"
    assert [tuple(color["rgb"]) for color in manifest["palette"]] == [
        (0, 0, 0),
        (255, 255, 255),
        (0, 255, 0),
        (0, 0, 255),
        (255, 0, 0),
        (255, 255, 0),
    ]
    assert manifest["files"][0]["size"] == 192_000


def test_failed_release_does_not_replace_latest(tmp_path):
    publisher = AtomicReleasePublisher(tmp_path / "releases")
    first = publisher.publish([("photo-1", Image.new("RGB", (480, 800), "white"))])
    with pytest.raises(ValueError):
        publisher.publish([("broken", Image.new("RGB", (100, 100), "white"))])
    assert (tmp_path / "releases" / "latest").read_text() == first["release_id"]


def test_invalid_release_contract_does_not_leak_temporary_directory(tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    with pytest.raises(ValueError, match="Release 類型"):
        publisher.publish(
            [("photo", Image.new("RGB", (480, 800), "white"))],
            release_kind="invalid",
        )
    assert not [path for path in root.iterdir() if path.name.endswith(".tmp")]


def test_preencoded_stock_release_uses_direct_idempotency_index_and_ttl_marker(monkeypatch, tmp_path):
    publisher = AtomicReleasePublisher(tmp_path / "releases")
    payload_path = tmp_path / "payload.bin"
    preview_path = tmp_path / "preview.png"
    payload_path.write_bytes(bytes(96_000))
    Image.new("RGB", (480, 800), "white").save(preview_path)
    manifest = publisher.publish_preencoded(
        source_photo_id="simulator-upload",
        payload_path=payload_path,
        preview_path=preview_path,
        profile_key="safe_4c",
        dither="none",
        color_distance="rgb",
        dither_strength=0,
        linear_light=True,
        palette=[],
        palette_version="test",
        metadata={
            "idempotency_key": "stock-index-key",
            "transport": "stock_direct",
            "stock_direct": True,
            "stock_direct_device_id": "stock-device",
        },
    )
    options = manifest["render_options"]
    assert options["stock_direct_expires_at"]
    assert any(
        (tmp_path / "releases" / directory / f"{manifest['release_id']}.json").is_file()
        for directory in (STOCK_DIRECT_TEST_DIRECTORY, STOCK_DIRECT_TEST_DEFERRED_DIRECTORY)
    )
    monkeypatch.setattr(
        publisher,
        "list",
        lambda: pytest.fail("idempotency lookup must not scan every release"),
    )
    assert publisher.find_device_test_by_idempotency("stock-index-key") == manifest


def _publish_preencoded_test(
    publisher: AtomicReleasePublisher,
    tmp_path: Path,
    *,
    idempotency_key: str,
    transport: str = "custom",
):
    payload_path = tmp_path / f"{idempotency_key}.bin"
    preview_path = tmp_path / f"{idempotency_key}.png"
    payload_path.write_bytes(bytes(96_000))
    Image.new("RGB", (480, 800), "white").save(preview_path)
    return publisher.publish_preencoded(
        source_photo_id="legacy-test",
        payload_path=payload_path,
        preview_path=preview_path,
        profile_key="safe_4c",
        dither="none",
        color_distance="rgb",
        dither_strength=0,
        linear_light=False,
        palette=[],
        palette_version="test",
        metadata={
            "idempotency_key": idempotency_key,
            "transport": transport,
            "stock_direct": transport == "stock_direct",
            "stock_direct_device_id": "stock-device" if transport == "stock_direct" else None,
        },
    )


def test_legacy_device_test_idempotency_is_backfilled_once_and_reused(monkeypatch, tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    manifest = _publish_preencoded_test(publisher, tmp_path, idempotency_key="legacy-idempotency")
    index = publisher._device_test_index_path("legacy-idempotency")
    index.unlink()
    migration = root / DEVICE_TEST_INDEX_MIGRATION_DIRECTORY
    if migration.exists():
        migration.rmdir()
    monkeypatch.setattr(
        publisher,
        "list",
        lambda: pytest.fail("legacy recovery must stream once without list/sort"),
    )

    recovered = publisher.find_device_test_by_idempotency("legacy-idempotency")

    assert recovered == manifest
    assert json.loads(index.read_text(encoding="utf-8"))["release_id"] == manifest["release_id"]
    assert len([path for path in root.iterdir() if (path / "manifest.json").is_file()]) == 1
    monkeypatch.setattr(
        publisher,
        "_backfill_legacy_device_test_indexes",
        lambda: pytest.fail("second lookup must use the O(1) index path"),
    )
    assert publisher.find_device_test_by_idempotency("legacy-idempotency") == manifest


def test_legacy_index_recovery_skips_corrupt_wrong_key_and_formal_releases(tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    valid = _publish_preencoded_test(publisher, tmp_path, idempotency_key="other-key")
    publisher._device_test_index_path("other-key").unlink()
    (root / valid["release_id"] / "manifest.json").write_text("{", encoding="utf-8")
    publisher.publish(
        [("formal", Image.new("RGB", (480, 800), "white"))],
        activate=False,
        metadata={"idempotency_key": "formal-key", "transport": "custom"},
    )

    assert publisher.find_device_test_by_idempotency("missing-key") is None
    assert not publisher._device_test_index_path("other-key").exists()
    assert not publisher._device_test_index_path("formal-key").exists()


def _rewrite_legacy_candidate(
    root: Path,
    manifest: dict,
    *,
    idempotency_key: str,
    created_at: str,
) -> dict:
    path = root / manifest["release_id"] / "manifest.json"
    candidate = json.loads(path.read_text(encoding="utf-8"))
    candidate["created_at"] = created_at
    candidate["render_options"]["idempotency_key"] = idempotency_key
    path.write_text(json.dumps(candidate), encoding="utf-8")
    return candidate


def _reset_device_test_index_migration(root: Path) -> None:
    shutil.rmtree(root / DEVICE_TEST_INDEX_DIRECTORY, ignore_errors=True)
    shutil.rmtree(root / DEVICE_TEST_INDEX_MIGRATION_DIRECTORY, ignore_errors=True)


def _migration_state(root: Path) -> dict:
    return json.loads(
        (root / DEVICE_TEST_INDEX_MIGRATION_DIRECTORY / "state.json").read_text(encoding="utf-8")
    )


def test_device_test_index_migration_is_versioned_and_ignores_release_root_mtime(monkeypatch, tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    manifest = _publish_preencoded_test(publisher, tmp_path, idempotency_key="legacy-v1")
    _reset_device_test_index_migration(root)

    assert publisher.find_device_test_by_idempotency("legacy-v1") == manifest
    state = _migration_state(root)
    assert state == {
        "schema_version": 1,
        "migration_version": DEVICE_TEST_INDEX_MIGRATION_VERSION,
        "complete": True,
        "completed_at": state["completed_at"],
    }
    os.utime(root, None)
    publisher._device_test_index_path("legacy-v1").unlink()
    monkeypatch.setattr(
        os,
        "scandir",
        lambda *_args, **_kwargs: pytest.fail("completed migration must not rescan root"),
    )

    assert AtomicReleasePublisher(root).find_device_test_by_idempotency("missing-key") is None


@pytest.mark.parametrize(
    "state",
    [
        None,
        {"complete": True},
        {
            "schema_version": 1,
            "migration_version": DEVICE_TEST_INDEX_MIGRATION_VERSION - 1,
            "complete": True,
            "completed_at": "2026-08-09T00:00:00+00:00",
        },
    ],
)
def test_missing_malformed_or_old_migration_state_reruns_once(state, tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    manifest = _publish_preencoded_test(publisher, tmp_path, idempotency_key="state-rerun")
    _reset_device_test_index_migration(root)
    if state is not None:
        state_root = root / DEVICE_TEST_INDEX_MIGRATION_DIRECTORY
        state_root.mkdir()
        (state_root / "state.json").write_text(json.dumps(state), encoding="utf-8")

    assert publisher.find_device_test_by_idempotency("state-rerun") == manifest
    assert _migration_state(root)["migration_version"] == DEVICE_TEST_INDEX_MIGRATION_VERSION


def test_malformed_migration_state_reruns_and_is_replaced(tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    manifest = _publish_preencoded_test(publisher, tmp_path, idempotency_key="bad-state")
    _reset_device_test_index_migration(root)
    state_root = root / DEVICE_TEST_INDEX_MIGRATION_DIRECTORY
    state_root.mkdir()
    (state_root / "state.json").write_text("{", encoding="utf-8")
    assert publisher.find_device_test_by_idempotency("bad-state") == manifest
    assert _migration_state(root)["complete"] is True


def test_transient_legacy_read_failure_does_not_mark_migration_complete(monkeypatch, tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    manifest = _publish_preencoded_test(publisher, tmp_path, idempotency_key="transient")
    _reset_device_test_index_migration(root)
    original_validate = publisher.validate

    def fail_candidate(release_id):
        if release_id == manifest["release_id"]:
            raise OSError("transient read failure")
        return original_validate(release_id)

    monkeypatch.setattr(publisher, "validate", fail_candidate)
    assert publisher.find_device_test_by_idempotency("transient") is None
    assert not (root / DEVICE_TEST_INDEX_MIGRATION_DIRECTORY / "state.json").exists()


def test_migration_restart_after_state_commit_failure_is_deterministic(monkeypatch, tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    expected = _publish_preencoded_test(publisher, tmp_path, idempotency_key="crash-rerun")
    _reset_device_test_index_migration(root)
    original_atomic_json = publisher._atomic_json

    def fail_state(path, payload):
        if path.name == "state.json":
            raise OSError("simulated crash before completion")
        return original_atomic_json(path, payload)

    monkeypatch.setattr(publisher, "_atomic_json", fail_state)
    assert publisher.find_device_test_by_idempotency("crash-rerun") is None
    assert not (root / DEVICE_TEST_INDEX_MIGRATION_DIRECTORY / "state.json").exists()
    monkeypatch.setattr(publisher, "_atomic_json", original_atomic_json)
    assert publisher.find_device_test_by_idempotency("force-rerun-miss") is None
    assert _migration_state(root)["complete"] is True
    assert publisher.find_device_test_by_idempotency("crash-rerun") == expected


def test_legacy_migration_ignores_wrong_transport_candidate(tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    manifest = _publish_preencoded_test(publisher, tmp_path, idempotency_key="wrong-transport")
    path = root / manifest["release_id"] / "manifest.json"
    candidate = json.loads(path.read_text(encoding="utf-8"))
    candidate["render_options"]["transport"] = "formal"
    path.write_text(json.dumps(candidate), encoding="utf-8")
    _reset_device_test_index_migration(root)
    assert publisher.find_device_test_by_idempotency("wrong-transport") is None
    assert not publisher._device_test_index_path("wrong-transport").exists()


def test_new_index_after_completed_migration_uses_hot_path_without_scan(monkeypatch, tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    assert publisher.find_device_test_by_idempotency("initial-miss") is None
    manifest = _publish_preencoded_test(publisher, tmp_path, idempotency_key="new-fast-path")
    monkeypatch.setattr(
        publisher,
        "_backfill_legacy_device_test_indexes",
        lambda: pytest.fail("normal indexed publish must remain O(1)"),
    )
    assert publisher.find_device_test_by_idempotency("new-fast-path") == manifest


def test_legacy_duplicate_selection_is_newest_and_filesystem_order_independent(monkeypatch, tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    older = _publish_preencoded_test(publisher, tmp_path, idempotency_key="older-source")
    newer = _publish_preencoded_test(publisher, tmp_path, idempotency_key="newer-source")
    _rewrite_legacy_candidate(
        root,
        older,
        idempotency_key="duplicate-key",
        created_at="2026-08-08T00:00:00+00:00",
    )
    expected = _rewrite_legacy_candidate(
        root,
        newer,
        idempotency_key="duplicate-key",
        created_at="2026-08-09T00:00:00+00:00",
    )
    _reset_device_test_index_migration(root)
    original_scandir = os.scandir

    class ReversedEntries:
        def __init__(self, entries):
            self.entries = entries

        def __enter__(self):
            return iter(reversed(self.entries))

        def __exit__(self, *_args):
            return False

    def reversed_root(path):
        if Path(path) == root:
            with original_scandir(path) as entries:
                return ReversedEntries(list(entries))
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", reversed_root)
    assert publisher.find_device_test_by_idempotency("duplicate-key") == expected


def test_legacy_duplicate_skips_malformed_newest_and_uses_valid_older(tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    older = _publish_preencoded_test(publisher, tmp_path, idempotency_key="valid-source")
    newer = _publish_preencoded_test(publisher, tmp_path, idempotency_key="broken-source")
    expected = _rewrite_legacy_candidate(
        root,
        older,
        idempotency_key="fallback-key",
        created_at="2026-08-08T00:00:00+00:00",
    )
    _rewrite_legacy_candidate(
        root,
        newer,
        idempotency_key="fallback-key",
        created_at="2026-08-09T00:00:00+00:00",
    )
    (root / newer["release_id"] / "manifest.json").write_text("{", encoding="utf-8")
    _reset_device_test_index_migration(root)
    assert publisher.find_device_test_by_idempotency("fallback-key") == expected


def test_legacy_duplicate_created_at_tie_uses_release_id_lexically(tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    first = _publish_preencoded_test(publisher, tmp_path, idempotency_key="tie-source-a")
    second = _publish_preencoded_test(publisher, tmp_path, idempotency_key="tie-source-b")
    candidates = [
        _rewrite_legacy_candidate(
            root,
            manifest,
            idempotency_key="tie-key",
            created_at="2026-08-09T00:00:00+00:00",
        )
        for manifest in (first, second)
    ]
    _reset_device_test_index_migration(root)
    expected = max(candidates, key=lambda item: item["release_id"])
    assert publisher.find_device_test_by_idempotency("tie-key") == expected


@pytest.mark.parametrize("corruption", ["malformed", "oversized", "symlink"])
def test_invalid_device_test_index_is_quarantined_without_following_links(corruption, tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    index = publisher._device_test_index_path("corrupt-index")
    index.parent.mkdir()
    state_root = root / DEVICE_TEST_INDEX_MIGRATION_DIRECTORY
    state_root.mkdir()
    (state_root / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "migration_version": DEVICE_TEST_INDEX_MIGRATION_VERSION,
                "complete": True,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    outside = tmp_path / "outside-index"
    outside.write_text("preserve", encoding="utf-8")
    if corruption == "malformed":
        index.write_text("{", encoding="utf-8")
    elif corruption == "oversized":
        index.write_bytes(b"x" * (64 * 1024 + 1))
    else:
        index.symlink_to(outside)

    assert publisher.find_device_test_by_idempotency("corrupt-index") is None
    assert not index.exists()
    assert any((root / DEVICE_TEST_INDEX_QUARANTINE_DIRECTORY).iterdir())
    assert outside.read_text(encoding="utf-8") == "preserve"


def test_stale_index_wrong_target_is_quarantined_then_replacement_is_usable(tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    manifest = _publish_preencoded_test(publisher, tmp_path, idempotency_key="stale-target")
    index = publisher._device_test_index_path("stale-target")
    (root / manifest["release_id"] / "photo_1.bin").unlink()
    state_root = root / DEVICE_TEST_INDEX_MIGRATION_DIRECTORY
    state_root.mkdir()
    (state_root / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "migration_version": DEVICE_TEST_INDEX_MIGRATION_VERSION,
                "complete": True,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    assert publisher.find_device_test_by_idempotency("stale-target") is None
    assert not index.exists()
    shutil.rmtree(root / manifest["release_id"])
    replacement = _publish_preencoded_test(publisher, tmp_path, idempotency_key="stale-target")
    assert publisher.find_device_test_by_idempotency("stale-target") == replacement


@pytest.mark.parametrize("target_kind", ["wrong_key", "formal"])
def test_index_target_contract_mismatch_is_quarantined(target_kind, tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    queried_key = f"contract-{target_kind}"
    if target_kind == "wrong_key":
        manifest = _publish_preencoded_test(publisher, tmp_path, idempotency_key="different-key")
    else:
        manifest = publisher.publish(
            [("formal", Image.new("RGB", (480, 800), "white"))],
            activate=False,
            metadata={"idempotency_key": queried_key, "transport": "custom"},
        )
    index = publisher._device_test_index_path(queried_key)
    publisher._atomic_json(
        index,
        {"release_id": manifest["release_id"], "idempotency_key": queried_key},
    )
    state_root = root / DEVICE_TEST_INDEX_MIGRATION_DIRECTORY
    state_root.mkdir()
    (state_root / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "migration_version": DEVICE_TEST_INDEX_MIGRATION_VERSION,
                "complete": True,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    assert publisher.find_device_test_by_idempotency(queried_key) is None
    assert not index.exists()
    assert any((root / DEVICE_TEST_INDEX_QUARANTINE_DIRECTORY).iterdir())


def test_stale_index_quarantine_never_removes_concurrent_replacement(tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    index = publisher._device_test_index_path("replacement-safety")
    index.parent.mkdir()
    index.write_text("{", encoding="utf-8")
    _parsed, identity, _reason = publisher._observe_device_test_index(index)
    replacement = {"release_id": "replacement", "idempotency_key": "replacement-safety"}
    publisher._atomic_json(index, replacement)

    assert not publisher._quarantine_observed_index(index, identity, "stale observation")
    assert json.loads(index.read_text(encoding="utf-8")) == replacement


def test_failed_stock_marker_transaction_rolls_back_only_new_index(monkeypatch, tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    original_create = publisher._atomic_json_create
    calls = 0

    def fail_marker(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("marker write failed")
        return original_create(path, payload)

    monkeypatch.setattr(publisher, "_atomic_json_create", fail_marker)
    with pytest.raises(OSError, match="marker write failed"):
        _publish_preencoded_test(
            publisher, tmp_path, idempotency_key="transaction-key", transport="stock_direct"
        )
    assert not publisher._device_test_index_path("transaction-key").exists()
    assert not any(
        list((root / directory).glob("*.json"))
        for directory in (STOCK_DIRECT_TEST_DIRECTORY, STOCK_DIRECT_TEST_DEFERRED_DIRECTORY)
        if (root / directory).exists()
    )
    assert not [path for path in root.iterdir() if (path / "manifest.json").is_file()]


def test_publication_failure_leaves_no_marker_and_preserves_preexisting_index(tmp_path):
    root = tmp_path / "releases"
    publisher = AtomicReleasePublisher(root)
    original = _publish_preencoded_test(
        publisher, tmp_path, idempotency_key="preexisting-index", transport="stock_direct"
    )
    index = publisher._device_test_index_path("preexisting-index")
    before = index.read_bytes()

    with pytest.raises(FileExistsError):
        _publish_preencoded_test(
            publisher, tmp_path, idempotency_key="preexisting-index", transport="stock_direct"
        )

    assert index.read_bytes() == before
    assert publisher.find_device_test_by_idempotency("preexisting-index") == original
    releases = [path for path in root.iterdir() if (path / "manifest.json").is_file()]
    assert [path.name for path in releases] == [original["release_id"]]


def test_custom_assignment_store_rejects_symlink_root(tmp_path):
    release_root = tmp_path / "releases"
    outside = tmp_path / "outside-assignments"
    release_root.mkdir()
    outside.mkdir()
    (release_root / ".device-tests").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="assignment store"):
        DeviceTestReleaseStore(release_root)

    assert list(outside.iterdir()) == []


def test_automatic_release_candidates_respect_configured_memory_threshold(app, tmp_path):
    app.extensions["inktime_settings_repository"].update(
        "analysis.execution_mode", "automatic_ai", changed_by="tester", source_ip="127.0.0.1"
    )
    photos = app.extensions["inktime_photo_repository"]
    root = Path(tmp_path / "photos")
    root.mkdir()
    for filename in ("80.jpg", "70.jpg", "60.jpg"):
        Image.new("RGB", (32, 32), "white").save(root / filename)
    library_id = photos.ensure_library("測試照片", root)
    now = "2026-07-17T00:00:00+00:00"
    with app.extensions["inktime_database"].session() as connection:
        connection.executemany(
            """
            INSERT INTO photos(id,library_id,relative_path,status,created_at,updated_at)
            VALUES (?,?,?,'discovered',?,?)
            """,
            [
                ("photo-80", library_id, "80.jpg", now, now),
                ("photo-70", library_id, "70.jpg", now, now),
                ("photo-60", library_id, "60.jpg", now, now),
            ],
        )

    for photo_id, memory_score in (("photo-80", 80), ("photo-70", 70), ("photo-60", 60)):
        result = {
            "schema_version": "1.0",
            "caption": "測試",
            "types": ["風景"],
            "memory_score": memory_score,
            "beauty_score": 50,
            "technical_quality_score": 50,
            "emotion_score": 50,
            "side_caption": "",
            "should_keep": True,
            "sensitive": False,
            "reason": "測試",
        }
        photos.save_analysis(photo_id, None, "test", "test", "test", result, "{}")

    render_service = app.extensions["inktime_render_service"]
    assert render_service.select_candidates() == ["photo-80", "photo-70"]

    app.extensions["inktime_settings_repository"].update(
        "render.memory_threshold", 75, changed_by="tester", source_ip="127.0.0.1"
    )
    assert render_service.select_candidates() == ["photo-80"]


def test_history_today_is_selected_before_higher_ranked_fallback(app, tmp_path):
    app.extensions["inktime_settings_repository"].update(
        "analysis.execution_mode", "automatic_ai", changed_by="tester", source_ip="127.0.0.1"
    )
    photos = app.extensions["inktime_photo_repository"]
    root = tmp_path / "history-today"
    root.mkdir()
    library_id = photos.ensure_library("歷年今日", root)
    now = "2026-07-20T00:00:00+00:00"
    entries = [
        ("exact-old", "exact.jpg", "2021-07-20T10:00:00", 78),
        ("nearby-old", "nearby.jpg", "2020-07-18T10:00:00", 96),
        ("exact-current", "current.jpg", "2026-07-20T10:00:00", 99),
    ]
    for _photo_id, filename, _captured, _score in entries:
        Image.new("RGB", (32, 32), "white").save(root / filename)
    with app.extensions["inktime_database"].session() as connection:
        connection.executemany(
            """
            INSERT INTO photos(id,library_id,relative_path,status,captured_at,captured_date,
                captured_month_day,capture_date_status,e6_score,created_at,updated_at)
            VALUES (?,?,?,'analyzed',?,?,?,'valid',80,?,?)
            """,
            [
                (photo_id, library_id, path, captured, captured[:10], captured[5:10], now, now)
                for photo_id, path, captured, _ in entries
            ],
        )
    for photo_id, _path, _captured, score in entries:
        result = {
            "schema_version": 1,
            "caption": "測試",
            "types": ["日常"],
            "memory_score": score,
            "beauty_score": score,
            "technical_quality_score": score,
            "emotion_score": score,
            "side_caption": "歷年今日",
            "should_keep": True,
            "sensitive": False,
            "reason": "選片測試",
        }
        photos.save_analysis(photo_id, None, "test", "local", "test", result, "{}", ranking_score=score)

    details = app.extensions["inktime_render_service"].select_candidates_details(
        2, target_date=date(2026, 7, 20)
    )

    assert [row["id"] for row in details] == ["exact-old", "nearby-old"]
    assert [row["match_type"] for row in details] == ["exact_day", "nearby_day"]
    assert details[1]["day_distance"] == 2


def test_all_photo_frame_layouts_render_at_panel_size(app, tmp_path):
    root = tmp_path / "layouts"
    root.mkdir()
    Image.new("RGB", (900, 600), "#527f99").save(root / "frame.jpg")
    Image.new("RGB", (600, 900), "#a45b42").save(root / "frame-2.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("版型", root)
    now = "2026-07-20T00:00:00+00:00"
    with app.extensions["inktime_database"].session() as connection:
        connection.executemany(
            """
            INSERT INTO photos(
                id,library_id,relative_path,status,captured_at,crop_focus_x,crop_focus_y,
                crop_method,created_at,updated_at
            ) VALUES (?,?,?,'analyzed',?,0.75,0.4,'saliency',?,?)
            """,
            [
                (
                    "layout-photo",
                    library_id,
                    "frame.jpg",
                    "2020-07-20T12:00:00",
                    now,
                    now,
                ),
                (
                    "layout-photo-2",
                    library_id,
                    "frame-2.jpg",
                    "2021-07-20T12:00:00",
                    now,
                    now,
                ),
            ],
        )
    photos.save_analysis(
        "layout-photo",
        None,
        "test",
        "local",
        "test",
        {
            "schema_version": 1,
            "caption": "旅行回憶",
            "types": ["旅行"],
            "memory_score": 88,
            "beauty_score": 80,
            "technical_quality_score": 80,
            "emotion_score": 85,
            "side_caption": "把這一天留在相框裡。",
            "should_keep": True,
            "sensitive": False,
            "reason": "版型測試",
        },
        "{}",
    )
    photos.save_analysis(
        "layout-photo-2",
        None,
        "test",
        "local",
        "test",
        {
            "schema_version": 1,
            "caption": "第二張回憶",
            "types": ["日常"],
            "memory_score": 82,
            "beauty_score": 78,
            "technical_quality_score": 79,
            "emotion_score": 84,
            "side_caption": "一起填滿相框。",
            "should_keep": True,
            "sensitive": False,
            "reason": "雙照片版型測試",
        },
        "{}",
    )
    service = app.extensions["inktime_render_service"]
    for layout in (
        "full",
        "postcard",
        "photo_info",
        "photo_pair",
        "calendar",
        "weather_sensor",
    ):
        rendered = service.render_photo(
            "layout-photo",
            layout=layout,
            secondary_photo_id="layout-photo-2" if layout == "photo_pair" else None,
        )
        assert rendered.size == (480, 800), layout

    landscape = service.render_photo(
        "layout-photo",
        layout="full",
        orientation="landscape",
        fit_mode="contain",
    ).transpose(Image.Transpose.ROTATE_90)
    assert landscape.size == (800, 480)
    assert landscape.getpixel((0, 0)) == (255, 255, 255)

    pair = service.render_photo(
        "layout-photo",
        layout="photo_pair",
        secondary_photo_id="layout-photo-2",
        orientation="landscape",
        fit_mode="cover",
    ).transpose(Image.Transpose.ROTATE_90)
    assert pair.size == (800, 480)
    assert pair.getpixel((198, 240)) != pair.getpixel((602, 240))

    info = service.render_photo("layout-photo", layout="photo_info")
    assert info.getpixel((479, 799)) == (255, 255, 255)
    assert info.getpixel((479, 720)) == (255, 255, 255)
    quantized = encode_image(
        info,
        profile_key="gdep073e01_6c",
        dither="floyd_steinberg",
        color_distance="oklab",
        strength=1,
    ).preview
    assert quantized.getpixel((479, 799)) == (255, 255, 255)


def test_formal_caption_uses_builtin_traditional_font_without_fallback(app, tmp_path):
    photo_root = tmp_path / "caption-photos"
    photo_root.mkdir()
    Image.new("RGB", (80, 120), "#9db7cf").save(photo_root / "memory.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("短文案測試", photo_root)
    now = "2026-07-19T00:00:00+00:00"
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "INSERT INTO photos(id,library_id,relative_path,status,created_at,updated_at) "
            "VALUES (?,?,?,'analyzed',?,?)",
            ("caption-photo", library_id, "memory.jpg", now, now),
        )
    photos.save_analysis(
        "caption-photo",
        None,
        "test",
        "local",
        "test",
        {
            "schema_version": "1.0",
            "caption": "回憶",
            "types": ["日常"],
            "memory_score": 80,
            "beauty_score": 70,
            "technical_quality_score": 70,
            "emotion_score": 80,
            "side_caption": "把今天的風景，寫進明日的回憶。",
            "should_keep": True,
            "sensitive": False,
            "reason": "測試內建繁中字型",
        },
        "{}",
    )

    render_service = app.extensions["inktime_render_service"]
    rendered = render_service.render_photo("caption-photo")
    assert rendered.size == (480, 800)
    assert app.extensions["inktime_settings_repository"].get("render.font_path") == "builtin:iansui"

    app.extensions["inktime_settings_repository"].update(
        "render.font_path", "", changed_by="tester", source_ip="127.0.0.1"
    )
    with pytest.raises(FontCoverageError, match="尚未設定"):
        render_service.render_photo("caption-photo")


def test_formal_render_shows_nearest_city_when_photo_has_gps(app, tmp_path):
    photo_root = tmp_path / "location-photos"
    photo_root.mkdir()
    Image.new("RGB", (80, 120), "#5f86a6").save(photo_root / "taipei.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("地點測試", photo_root)
    now = "2026-07-20T00:00:00+00:00"
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "INSERT INTO photos(id,library_id,relative_path,status,gps_lat,gps_lon,created_at,updated_at) "
            "VALUES (?,?,?,'analyzed',?,?,?,?)",
            ("location-photo", library_id, "taipei.jpg", 25.05306, 121.52639, now, now),
        )
    photos.save_analysis(
        "location-photo",
        None,
        "test",
        "local",
        "test",
        {
            "schema_version": 1,
            "caption": "臺北回憶",
            "types": ["旅行"],
            "memory_score": 80,
            "beauty_score": 70,
            "technical_quality_score": 70,
            "emotion_score": 80,
            "side_caption": "",
            "should_keep": True,
            "sensitive": False,
            "reason": "測試地點顯示",
        },
        "{}",
    )
    render_service = app.extensions["inktime_render_service"]
    photo = photos.get_with_path("location-photo")

    assert render_service.location_name(photo) == "臺北市"
    with_location = render_service.render_photo("location-photo")
    app.extensions["inktime_settings_repository"].update(
        "render.show_location", False, changed_by="tester", source_ip="127.0.0.1"
    )
    without_location = render_service.render_photo("location-photo")
    assert with_location.tobytes() != without_location.tobytes()
