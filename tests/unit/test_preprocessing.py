from __future__ import annotations

from PIL import Image

from inktime.app.domain.photos import PhotoPreprocessor, ThumbnailCache


def test_local_features_and_content_addressed_thumbnails(tmp_path):
    source = tmp_path / "photo.jpg"
    Image.new("RGB", (1200, 800), (180, 90, 40)).save(source)
    features = PhotoPreprocessor().analyze(source)
    assert len(features.sha256) == 64
    assert len(features.perceptual_hash) == 16
    assert len(features.difference_hash) == 16
    assert features.width == 1200
    assert 0 <= features.overexposed_ratio <= 1

    cache = ThumbnailCache(tmp_path / "cache")
    first = cache.get_or_create(source, features.sha256, 512)
    mtime = first.stat().st_mtime_ns
    second = cache.get_or_create(source, features.sha256, 512)
    assert first == second
    assert second.stat().st_mtime_ns == mtime
    with Image.open(second) as thumbnail:
        assert max(thumbnail.size) == 512
        assert thumbnail.size == (512, 341)
    assert cache.size_bytes() > 0
    assert cache.clear() == 1


def test_screenshot_filename_is_detected_without_exiftool(tmp_path):
    source = tmp_path / "螢幕快照 2026-07-20.png"
    Image.new("RGB", (900, 600), "white").save(source)

    features = PhotoPreprocessor().analyze(source)

    assert features.screenshot_likelihood >= 0.8
