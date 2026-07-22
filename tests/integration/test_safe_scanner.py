from __future__ import annotations

from hashlib import sha256
import json
import shutil

from PIL import Image
import pytest

from inktime.app.domain.photos import LocalPhotoFeatures, PhotoPreprocessor, ThumbnailCache
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.workers.scanner import PhotoScanner
from tests.unit.test_analysis_schema import valid_result


class FastPreprocessor:
    def __init__(self) -> None:
        self.calls = 0

    def analyze(
        self,
        path,
        *,
        include_metadata: bool = True,
        include_local_features: bool = True,
    ) -> LocalPhotoFeatures:
        self.calls += 1
        digest = sha256(path.read_bytes()).hexdigest()
        return LocalPhotoFeatures(
            sha256=digest,
            perceptual_hash=digest[:16] if include_local_features else None,
            difference_hash=digest[16:32] if include_local_features else None,
            width=16,
            height=16,
            format=path.suffix.lstrip(".").upper() or "JPG",
            orientation=1,
            camera_make=None,
            camera_model=None,
            lens_model=None,
            exif_json="{}" if include_metadata else None,
            captured_at=None,
            gps_lat=None,
            gps_lon=None,
            brightness=100.0 if include_local_features else None,
            contrast=10.0 if include_local_features else None,
            blur_score=2.0 if include_local_features else None,
            overexposed_ratio=0.0 if include_local_features else None,
            underexposed_ratio=0.0 if include_local_features else None,
            screenshot_likelihood=0.0 if include_local_features else None,
            crop_focus_x=0.5 if include_local_features else None,
            crop_focus_y=0.5 if include_local_features else None,
            crop_subject_left=0.0 if include_local_features else None,
            crop_subject_top=0.0 if include_local_features else None,
            crop_subject_right=1.0 if include_local_features else None,
            crop_subject_bottom=1.0 if include_local_features else None,
            crop_method="test" if include_local_features else None,
            crop_face_count=0 if include_local_features else None,
            e6_score=None,
            e6_contrast_score=None,
            e6_subject_score=None,
            e6_skin_score=None,
            e6_text_score=None,
            e6_skin_pixels=0 if include_local_features else None,
            metadata_complete=include_metadata,
            local_features_complete=include_local_features,
        )


def make_scanner(app, tmp_path, preprocessor=None):
    processor = preprocessor or FastPreprocessor()
    return (
        PhotoScanner(
            PhotoRepository(app.extensions["inktime_database"]),
            processor,
            ThumbnailCache(tmp_path / "thumbnails"),
        ),
        processor,
    )


def seed_files(root, count: int) -> None:
    root.mkdir()
    for index in range(count):
        (root / f"photo-{index:03d}.jpg").write_bytes(f"photo-{index}".encode())


def test_batch_database_failure_rolls_back_whole_write_batch(app, tmp_path):
    root = tmp_path / "photos"
    seed_files(root, 2)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_second_photo BEFORE INSERT ON photos
            WHEN NEW.relative_path='photo-001.jpg'
            BEGIN SELECT RAISE(ABORT, 'batch failure'); END
            """
        )
    scanner, _ = make_scanner(app, tmp_path)

    result = scanner.scan("交易測試", root, build_thumbnails=False, write_batch_size=500)

    assert result["processed"] == 0
    assert result["failed"] == 2
    assert result["warning_code"] == "SCAN-IO-002"
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0
        errors = connection.execute(
            "SELECT stage,error_code,masked_path FROM scan_errors ORDER BY id"
        ).fetchall()
    assert [(row["stage"], row["error_code"]) for row in errors] == [
        ("database", "SCAN-DB-001"),
        ("database", "SCAN-DB-001"),
    ]
    assert all("photo-" not in row["masked_path"] for row in errors)


def test_single_photo_failure_is_persisted_without_stopping_scan(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    Image.new("RGB", (40, 30), "green").save(root / "good.png")
    (root / "private-family-name.jpg").write_bytes(b"broken")
    scanner, _ = make_scanner(app, tmp_path, PhotoPreprocessor())

    result = scanner.scan("錯誤測試", root, build_thumbnails=False)

    assert result["processed"] == 1
    assert result["failed"] == 1
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1
        error = connection.execute(
            "SELECT stage,error_code,exception_type,retryable,masked_path FROM scan_errors"
        ).fetchone()
    assert error["stage"] == "preprocess"
    assert error["error_code"] == "SCAN-PHOTO-001"
    assert error["exception_type"]
    assert "private-family-name" not in error["masked_path"]


def test_existing_photo_preprocess_failure_is_seen_and_marked_for_retry(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    target = root / "retry.png"
    Image.new("RGB", (40, 30), "green").save(target)
    scanner, _ = make_scanner(app, tmp_path, PhotoPreprocessor())
    scanner.scan("重試狀態", root, build_thumbnails=False)
    with app.extensions["inktime_database"].session() as connection:
        photo_id = str(connection.execute("SELECT id FROM photos").fetchone()[0])
    target.write_bytes(b"temporarily-corrupt-image")

    result = scanner.scan("重試狀態", root, build_thumbnails=False)

    assert result["failed"] == 1
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            """
            SELECT lifecycle_status,metadata_status,local_features_status,last_seen_scan_id
            FROM photos WHERE id=?
            """,
            (photo_id,),
        ).fetchone()
    assert tuple(row) == ("active", "failed", "failed", result["scan_id"])


def test_missing_is_safe_and_reappearing_photo_preserves_analysis(app, tmp_path):
    root = tmp_path / "photos"
    seed_files(root, 20)
    scanner, _ = make_scanner(app, tmp_path)
    scanner.scan("家庭相簿", root, build_thumbnails=False)
    target = root / "photo-000.jpg"
    payload = target.read_bytes()
    repository = app.extensions["inktime_photo_repository"]
    with app.extensions["inktime_database"].session() as connection:
        photo_id = connection.execute(
            "SELECT id FROM photos WHERE relative_path='photo-000.jpg'"
        ).fetchone()[0]
    analysis = valid_result()
    repository.save_analysis(
        str(photo_id), None, "local", "local", "local", analysis, json.dumps(analysis)
    )
    target.unlink()

    missing = scanner.scan("家庭相簿", root, build_thumbnails=False)

    assert missing["missing_marked"] == 1
    with app.extensions["inktime_database"].session() as connection:
        photo = connection.execute(
            "SELECT lifecycle_status,missing_since FROM photos WHERE id=?", (photo_id,)
        ).fetchone()
    assert photo["lifecycle_status"] == "missing"
    assert photo["missing_since"]

    target.write_bytes(payload)
    restored = scanner.scan("家庭相簿", root, build_thumbnails=False)
    assert restored["restored"] == 1
    with app.extensions["inktime_database"].session() as connection:
        photo = connection.execute(
            "SELECT id,lifecycle_status,missing_since FROM photos WHERE relative_path='photo-000.jpg'"
        ).fetchone()
        analysis_count = connection.execute(
            "SELECT COUNT(*) FROM photo_analysis WHERE photo_id=?", (photo_id,)
        ).fetchone()[0]
    assert tuple(photo) == (photo_id, "active", None)
    assert analysis_count == 1


def test_mount_failure_and_cancelled_scan_never_mark_library_missing(app, tmp_path):
    root = tmp_path / "photos"
    seed_files(root, 4)
    scanner, _ = make_scanner(app, tmp_path)
    scanner.scan("NAS", root, build_thumbnails=False)
    hidden = tmp_path / "unmounted"
    root.rename(hidden)

    with pytest.raises(FileNotFoundError, match="SCAN-001"):
        scanner.scan("NAS", root, build_thumbnails=False)
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM photos WHERE lifecycle_status='missing'"
        ).fetchone()[0] == 0

    hidden.rename(root)
    for path in root.glob("*.jpg"):
        path.unlink()
    cancelled = scanner.scan(
        "NAS", root, build_thumbnails=False, cancel_requested=lambda: True
    )
    assert cancelled["cancelled"] is True
    assert cancelled["reconciliation_status"] == "skipped"
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM photos WHERE lifecycle_status='missing'"
        ).fetchone()[0] == 0


def test_stat_io_error_continues_scan_but_blocks_missing_reconciliation(
    app, tmp_path, monkeypatch
):
    root = tmp_path / "photos"
    seed_files(root, 1)
    scanner, _ = make_scanner(app, tmp_path)
    scanner.scan("I/O 防護", root, build_thumbnails=False)
    (root / "photo-000.jpg").unlink()

    class BrokenPath:
        def relative_to(self, _root):
            return self

        def as_posix(self):
            return "photo-000.jpg"

        def stat(self):
            raise OSError("temporary NAS I/O failure")

        def __str__(self):
            return "photo-000.jpg"

    monkeypatch.setattr(
        "inktime.app.workers.scanner.iter_media",
        lambda _root, *, on_error=None: iter([(BrokenPath(), "image")]),
    )
    result = scanner.scan("I/O 防護", root, build_thumbnails=False)

    assert result["failed"] == 1
    assert result["warning_code"] == "SCAN-IO-002"
    assert result["reconciliation_status"] == "skipped"
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM photos WHERE lifecycle_status='missing'"
        ).fetchone()[0] == 0


def test_large_missing_drop_requires_manual_confirmation(app, tmp_path):
    root = tmp_path / "photos"
    seed_files(root, 2)
    scanner, _ = make_scanner(app, tmp_path)
    scanner.scan("家庭相簿", root, build_thumbnails=False)
    (root / "photo-000.jpg").unlink()

    result = scanner.scan("家庭相簿", root, build_thumbnails=False)

    assert result["candidate_missing"] == 1
    assert result["missing_marked"] == 0
    assert result["warning_code"] == "SCAN-MISSING-THRESHOLD"
    assert result["reconciliation_status"] == "confirmation_required"
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM scan_missing_candidates WHERE scan_id=?",
            (result["scan_id"],),
        ).fetchone()[0] == 1
    assert app.extensions["inktime_photo_repository"].confirm_missing(result["scan_id"]) == 1
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM photos WHERE lifecycle_status='missing'"
        ).fetchone()[0] == 1


def test_move_keeps_id_analysis_favorite_history_and_does_not_rebuild_thumbnail(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    source = root / "old.png"
    Image.new("RGB", (40, 30), "navy").save(source)
    scanner, _ = make_scanner(app, tmp_path)
    scanner.scan("家庭相簿", root, build_thumbnails=True)
    repository = app.extensions["inktime_photo_repository"]
    with app.extensions["inktime_database"].session() as connection:
        photo_id = connection.execute("SELECT id FROM photos").fetchone()[0]
        connection.execute("UPDATE photos SET favorite=1 WHERE id=?", (photo_id,))
        connection.execute(
            "INSERT INTO photo_events(photo_id,event,changes_json,created_at) VALUES (?,'displayed','{}',datetime('now'))",
            (photo_id,),
        )
    analysis = valid_result()
    repository.save_analysis(
        str(photo_id), None, "local", "local", "local", analysis, json.dumps(analysis)
    )
    source.rename(root / "new.png")

    def thumbnail_must_not_run(*_args, **_kwargs):
        raise AssertionError("搬移不應重建縮圖")

    scanner.thumbnails.get_or_create = thumbnail_must_not_run
    result = scanner.scan("家庭相簿", root, build_thumbnails=True)

    assert result["moved"] == 1
    with app.extensions["inktime_database"].session() as connection:
        photo = connection.execute(
            "SELECT id,relative_path,favorite,status FROM photos"
        ).fetchone()
        analysis_count = connection.execute("SELECT COUNT(*) FROM photo_analysis").fetchone()[0]
        history_count = connection.execute("SELECT COUNT(*) FROM photo_events").fetchone()[0]
    assert tuple(photo) == (photo_id, "new.png", 1, "analyzed")
    assert analysis_count == 1
    assert history_count == 1


def test_simultaneous_same_content_is_duplicate_not_move(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    first = root / "first.jpg"
    first.write_bytes(b"same-content")
    scanner, _ = make_scanner(app, tmp_path)
    scanner.scan("家庭相簿", root, build_thumbnails=False)
    shutil.copy2(first, root / "second.jpg")

    result = scanner.scan("家庭相簿", root, build_thumbnails=False)

    assert result["moved"] == 0
    assert result["duplicates"] == 1
    with app.extensions["inktime_database"].session() as connection:
        rows = connection.execute(
            "SELECT id,relative_path,duplicate_group_id FROM photos ORDER BY relative_path"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["duplicate_group_id"] == rows[1]["duplicate_group_id"]
    assert rows[0]["duplicate_group_id"]


def test_unchanged_photo_skips_preprocessor_and_thumbnail_even_when_enabled(app, tmp_path):
    root = tmp_path / "photos"
    seed_files(root, 1)
    scanner, processor = make_scanner(app, tmp_path)
    scanner.scan("快速路徑", root, build_thumbnails=False)

    def thumbnail_must_not_run(*_args, **_kwargs):
        raise AssertionError("未變照片不得讀取或建立縮圖")

    scanner.thumbnails.get_or_create = thumbnail_must_not_run
    result = scanner.scan("快速路徑", root, build_thumbnails=True)

    assert result["skipped"] == 1
    assert result["processed"] == 0
    assert processor.calls == 1


def test_newer_scan_blocks_confirmation_of_saved_old_missing_candidates(app, tmp_path):
    root = tmp_path / "photos"
    seed_files(root, 2)
    scanner, _ = make_scanner(app, tmp_path)
    scanner.scan("候選快照", root, build_thumbnails=False)
    (root / "photo-000.jpg").unlink()

    old_scan = scanner.scan("候選快照", root, build_thumbnails=False)
    new_scan = scanner.scan("候選快照", root, build_thumbnails=False)

    repository = app.extensions["inktime_photo_repository"]
    with pytest.raises(ValueError, match="SCAN-MISSING-004"):
        repository.confirm_missing(old_scan["scan_id"])
    assert repository.confirm_missing(new_scan["scan_id"]) == 1


def test_content_change_invalidates_sections_not_run_by_partial_mode(app, tmp_path):
    root = tmp_path / "photos"
    seed_files(root, 1)
    scanner, _ = make_scanner(app, tmp_path)
    scanner.scan("部分模式", root, mode="full", build_thumbnails=False)
    repository = app.extensions["inktime_photo_repository"]
    with app.extensions["inktime_database"].session() as connection:
        photo_id = str(connection.execute("SELECT id FROM photos").fetchone()[0])
    analysis = valid_result()
    repository.save_analysis(
        photo_id, None, "local", "local", "local", analysis, json.dumps(analysis)
    )

    photo = root / "photo-000.jpg"
    photo.write_bytes(b"changed-content-for-metadata-only")
    scanner.scan("部分模式", root, mode="metadata-only", build_thumbnails=False)
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT metadata_status,local_features_status,perceptual_hash FROM photos"
        ).fetchone()
        assert connection.execute("SELECT COUNT(*) FROM photo_analysis").fetchone()[0] == 0
    assert tuple(row) == ("complete", "pending", None)

    scanner.scan("部分模式", root, mode="local-features-only", build_thumbnails=False)
    photo.write_bytes(b"changed-again-for-local-features-only-with-another-size")
    scanner.scan("部分模式", root, mode="local-features-only", build_thumbnails=False)
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT metadata_status,local_features_status,exif_json FROM photos"
        ).fetchone()
    assert tuple(row) == ("pending", "complete", None)


def test_scan_modes_fill_only_requested_sections(app, tmp_path):
    root = tmp_path / "photos"
    seed_files(root, 1)
    scanner, processor = make_scanner(app, tmp_path)

    metadata = scanner.scan("模式", root, mode="metadata-only", build_thumbnails=False)
    assert metadata["processed"] == 1
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT status,metadata_status,local_features_status FROM photos"
        ).fetchone()
    assert tuple(row) == ("discovered", "complete", "pending")

    local = scanner.scan("模式", root, mode="local-features-only", build_thumbnails=False)
    assert local["processed"] == 1
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT status,metadata_status,local_features_status FROM photos"
        ).fetchone()
    assert tuple(row) == ("preprocessed", "complete", "complete")

    assert scanner.scan("模式", root, mode="incremental", build_thumbnails=False)["skipped"] == 1
    assert scanner.scan("模式", root, mode="manual", build_thumbnails=False)["skipped"] == 1
    assert scanner.scan("模式", root, mode="full", build_thumbnails=False)["processed"] == 1
    assert processor.calls == 3
