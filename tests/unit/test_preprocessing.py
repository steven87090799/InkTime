from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

from PIL import Image
import pytest

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


def test_thumbnail_generation_is_single_flight_atomic_and_validated(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (800, 600), "teal").save(source)
    digest = sha256(source.read_bytes()).hexdigest()
    cache = ThumbnailCache(tmp_path / "cache")

    with ThreadPoolExecutor(max_workers=12) as executor:
        paths = list(executor.map(lambda _index: cache.get_or_create(source, digest, 512), range(24)))

    assert len(set(paths)) == 1
    destination = paths[0]
    with Image.open(destination) as image:
        assert image.format == "JPEG"
        assert max(image.size) == 512
        image.verify()
    assert not list(cache.root.glob("*.tmp"))

    destination.write_bytes(b"corrupt")
    assert cache.get_or_create(source, digest, 512) == destination
    with Image.open(destination) as image:
        assert image.format == "JPEG"
        image.verify()


def test_thumbnail_failure_cleans_unique_temporary_file(tmp_path):
    source = tmp_path / "broken.jpg"
    source.write_bytes(b"not an image")
    cache = ThumbnailCache(tmp_path / "cache")

    with pytest.raises(OSError):
        cache.get_or_create(source, sha256(source.read_bytes()).hexdigest(), 512)

    assert not list(cache.root.glob("*.tmp"))


def test_thumbnail_rejects_source_that_no_longer_matches_content_hash(tmp_path):
    source = tmp_path / "changed.png"
    Image.new("RGB", (80, 60), "red").save(source)
    stale_digest = sha256(source.read_bytes()).hexdigest()
    Image.new("RGB", (80, 60), "blue").save(source)
    cache = ThumbnailCache(tmp_path / "cache")

    with pytest.raises(OSError, match="THUMB-004"):
        cache.get_or_create(source, stale_digest, 512)

    assert not list(cache.root.glob("*.tmp"))
    assert not list(cache.root.glob("*.jpg"))


def test_screenshot_filename_is_detected_without_exiftool(tmp_path):
    source = tmp_path / "螢幕快照 2026-07-20.png"
    Image.new("RGB", (900, 600), "white").save(source)

    features = PhotoPreprocessor().analyze(source)

    assert features.screenshot_likelihood >= 0.8
