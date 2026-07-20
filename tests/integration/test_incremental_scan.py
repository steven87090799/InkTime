from __future__ import annotations

import json
import os

from PIL import Image

from inktime.app.domain.photos import PhotoPreprocessor, ThumbnailCache
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.workers.scanner import PhotoScanner
from tests.unit.test_analysis_schema import valid_result


class CountingPreprocessor:
    def __init__(self) -> None:
        self.delegate = PhotoPreprocessor()
        self.calls: list[str] = []

    def analyze(self, path):
        self.calls.append(path.name)
        return self.delegate.analyze(path)


def make_scanner(app, tmp_path):
    preprocessor = CountingPreprocessor()
    scanner = PhotoScanner(
        PhotoRepository(app.extensions["inktime_database"]),
        preprocessor,
        ThumbnailCache(tmp_path / "cache"),
    )
    return scanner, preprocessor


def test_unchanged_photo_skips_features_and_changed_content_invalidates_analysis(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    source = root / "memory.png"
    Image.new("RGB", (80, 60), "navy").save(source)
    scanner, preprocessor = make_scanner(app, tmp_path)

    first = scanner.scan("家庭相簿", root, build_thumbnails=False)
    assert first == {
        "library_id": first["library_id"],
        "checked": 1,
        "processed": 1,
        "skipped": 0,
        "new": 1,
        "changed": 0,
        "inherited": 0,
        "failed": 0,
        "excluded_videos": 0,
    }
    with app.extensions["inktime_database"].session() as connection:
        photo = connection.execute("SELECT id,sha256 FROM photos").fetchone()
    repository = app.extensions["inktime_photo_repository"]
    analysis = valid_result()
    repository.save_analysis(
        str(photo["id"]),
        None,
        "local",
        "local",
        "local",
        analysis,
        json.dumps(analysis, ensure_ascii=False),
    )

    unchanged = scanner.scan("家庭相簿", root, build_thumbnails=True)
    assert unchanged["checked"] == 1
    assert unchanged["processed"] == 0
    assert unchanged["skipped"] == 1
    assert preprocessor.calls == ["memory.png"]
    assert (tmp_path / "cache" / f"{photo['sha256']}-512.jpg").is_file()

    before_touch = source.stat()
    os.utime(
        source,
        ns=(before_touch.st_atime_ns, before_touch.st_mtime_ns + 1_000_000_000),
    )
    metadata_changed = scanner.scan("家庭相簿", root, build_thumbnails=False)
    assert metadata_changed["changed"] == 1
    assert metadata_changed["processed"] == 1
    with app.extensions["inktime_database"].session() as connection:
        preserved = connection.execute(
            "SELECT status,sha256 FROM photos WHERE id=?", (photo["id"],)
        ).fetchone()
        analysis_count = connection.execute(
            "SELECT COUNT(*) FROM photo_analysis WHERE photo_id=?", (photo["id"],)
        ).fetchone()[0]
    assert preserved["status"] == "analyzed"
    assert preserved["sha256"] == photo["sha256"]
    assert analysis_count == 1

    Image.new("RGB", (80, 60), "gold").save(source)
    changed_stat = source.stat()
    os.utime(
        source,
        ns=(changed_stat.st_atime_ns, changed_stat.st_mtime_ns + 1_000_000_000),
    )
    content_changed = scanner.scan("家庭相簿", root, build_thumbnails=False)
    assert content_changed["changed"] == 1
    assert content_changed["processed"] == 1
    assert preprocessor.calls == ["memory.png", "memory.png", "memory.png"]
    with app.extensions["inktime_database"].session() as connection:
        updated = connection.execute("SELECT id,status,sha256 FROM photos").fetchone()
        analysis_count = connection.execute("SELECT COUNT(*) FROM photo_analysis").fetchone()[0]
    assert updated["id"] == photo["id"]
    assert updated["status"] == "preprocessed"
    assert updated["sha256"] != photo["sha256"]
    assert analysis_count == 0


def test_new_path_is_processed_even_when_size_and_modified_time_match(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    original = root / "original.png"
    Image.new("RGB", (48, 48), "green").save(original)
    scanner, preprocessor = make_scanner(app, tmp_path)

    scanner.scan("家庭相簿", root, build_thumbnails=False)
    with app.extensions["inktime_database"].session() as connection:
        photo_id = connection.execute("SELECT id FROM photos").fetchone()[0]
    renamed = root / "renamed.png"
    original.rename(renamed)

    result = scanner.scan("家庭相簿", root, build_thumbnails=False)
    assert result["checked"] == 1
    assert result["new"] == 1
    assert result["processed"] == 1
    assert result["skipped"] == 0
    assert preprocessor.calls == ["original.png", "renamed.png"]
    with app.extensions["inktime_database"].session() as connection:
        photo = connection.execute("SELECT id,relative_path FROM photos").fetchone()
    assert photo["id"] == photo_id
    assert photo["relative_path"] == "renamed.png"


def test_scan_counts_videos_without_creating_photo_records(app, tmp_path):
    root = tmp_path / "mixed-media"
    root.mkdir()
    Image.new("RGB", (64, 64), "teal").save(root / "memory.jpg")
    (root / "live-photo.mov").write_bytes(b"not-decoded-because-video-is-excluded")
    scanner, _ = make_scanner(app, tmp_path)

    result = scanner.scan("混合媒體", root, build_thumbnails=False)

    assert result["checked"] == 1
    assert result["processed"] == 1
    assert result["excluded_videos"] == 1
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1


def test_new_analysis_job_selects_preprocessed_scan_results(app, tmp_path):
    root = tmp_path / "ready-for-analysis"
    root.mkdir()
    Image.new("RGB", (64, 64), "purple").save(root / "new.jpg")
    scanner, _ = make_scanner(app, tmp_path)
    scanner.scan("待分析", root, build_thumbnails=False)

    job_id = app.extensions["inktime_job_service"].create_analysis_job(
        name="新照片分析",
        strategy="local",
        settings={},
        created_by="tester",
        budget_limit=None,
    )

    assert app.extensions["inktime_job_repository"].get(job_id)["total_items"] == 1
