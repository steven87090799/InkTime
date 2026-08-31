from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import builtins
import errno
import multiprocessing
import os
from pathlib import Path
from queue import Empty
import struct
import threading

from PIL import Image, ImageOps
import pytest

from inktime.app.domain.photos import PhotoPreprocessor, ThumbnailCache
from inktime.app.domain.photos import formats
from inktime.app.workers.scanner import SUPPORTED_EXTENSIONS, VIDEO_EXTENSIONS, iter_media


def make_image(path: Path, *, orientation=1, size=(120, 80)):
    assert formats.ensure_image_codecs_registered(), "production HEIF decoder must be installed"
    with Image.new("RGB", size, "red") as image:
        image.paste("blue", (size[0] // 2, 0, size[0], size[1]))
        exif = image.getexif()
        exif[274] = orientation
        exif[271] = "Synthetic"
        image.save(path, exif=exif)
    return sha256(path.read_bytes()).hexdigest()


def test_single_source_format_contract_and_live_photo(tmp_path):
    assert SUPPORTED_EXTENSIONS is formats.SUPPORTED_IMAGE_EXTENSIONS
    assert SUPPORTED_EXTENSIONS == {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".heic",
        ".heif",
        ".tif",
        ".tiff",
        ".bmp",
    }
    for suffix in (".heic", ".mov", ".gif", ".dng"):
        (tmp_path / ("live" + suffix)).touch()
    assert dict((p.suffix, kind) for p, kind in iter_media(tmp_path)) == {
        ".heic": "image",
        ".mov": "video",
        ".gif": "video",
        ".dng": "unsupported_raw",
    }
    assert ".gif" in VIDEO_EXTENSIONS and ".dng" not in SUPPORTED_EXTENSIONS
    assert formats.is_supported_image_path("IPHONE.HEIC")


@pytest.mark.parametrize("suffix", sorted(formats.SUPPORTED_IMAGE_EXTENSIONS))
def test_supported_formats_preprocess_and_jpeg_derivatives(tmp_path, suffix):
    source = tmp_path / ("source" + suffix)
    digest = make_image(source)
    features = PhotoPreprocessor().analyze(source)
    assert features.sha256 == digest
    assert (features.width, features.height) == (120, 80)
    assert features.perceptual_hash and features.difference_hash
    assert features.brightness is not None and features.e6_score is not None
    if suffix in {".heic", ".heif"}:
        assert features.format == "HEIF" and features.camera_make == "Synthetic"
    cache = ThumbnailCache(tmp_path / "cache")
    paths = [cache.get_or_create(source, digest, size) for size in (512, 1024, 1600)]
    assert len(set(paths)) == 3
    for path in paths:
        with Image.open(path) as derivative:
            assert derivative.format == "JPEG" and derivative.mode == "RGB"
            assert derivative.getexif().get(274) is None
    assert sha256(source.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("suffix", [".jpg", ".heic", ".tiff"])
@pytest.mark.parametrize("orientation", [1, 6, 8])
def test_orientation_not_applied_twice(tmp_path, suffix, orientation):
    source = tmp_path / ("orientation" + suffix)
    digest = make_image(source, orientation=orientation)
    # HEIF encoder converts EXIF into container transforms; libheif applies
    # them on open. TIFF 12.3 also reports oriented size before pixel load.
    with Image.open(source) as opened:
        with ImageOps.exif_transpose(opened) as expected:
            expected = expected.convert("RGB")
    features = PhotoPreprocessor().analyze(source)
    assert features.orientation == orientation
    assert (features.width, features.height) == expected.size
    from inktime.app.services.rendering import RenderService

    renderer = RenderService.__new__(RenderService)
    rendered, _ = renderer._load_oriented_photo({"orientation": features.orientation}, source)
    assert rendered.size == expected.size
    assert rendered.getpixel((10, 10)) == expected.getpixel((10, 10))
    rendered.close()
    for size in (512, 1024, 1600):
        path = ThumbnailCache(tmp_path / "cache").get_or_create(source, digest, size)
        with Image.open(path) as output:
            assert output.size == expected.size
            assert output.getexif().get(274) is None
            for xy in [(10, 10), (output.width - 10, output.height - 10)]:
                assert (
                    max(abs(a - b) for a, b in zip(output.getpixel(xy), expected.getpixel(xy), strict=True))
                    < 12
                )


@pytest.mark.parametrize(
    "mode,suffix", [("RGBA", ".png"), ("LA", ".png"), ("P", ".png"), ("CMYK", ".jpg"), ("I;16", ".tiff")]
)
def test_color_modes_have_rgb_contract(tmp_path, mode, suffix):
    source = tmp_path / ("mode" + suffix)
    image = Image.new(mode, (32, 24))
    if mode == "P":
        image.info["transparency"] = 0
    if mode == "I;16":
        image = Image.new(mode, (32, 24), 32768)
    image.save(source)
    with formats.load_rgb(source) as result:
        assert result.mode == "RGB"
        if mode in {"RGBA", "LA", "P"}:
            assert result.getpixel((0, 0)) == (255, 255, 255)
        if mode == "I;16":
            assert result.getpixel((0, 0)) == (128, 128, 128)


def test_decoder_missing_is_explicit_and_registration_once(tmp_path, monkeypatch):
    import pillow_heif

    source = tmp_path / "source.heic"
    make_image(source)
    real = pillow_heif.register_heif_opener
    calls = []

    def counted(**kwargs):
        calls.append(kwargs)
        real(**kwargs)

    monkeypatch.setattr(formats, "_heif_available", None)
    monkeypatch.setattr(pillow_heif, "register_heif_opener", counted)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(lambda _: formats.ensure_image_codecs_registered(), range(16)))
    assert len(calls) == 1 and calls[0]["decode_threads"] == 1
    original_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "pillow_heif":
            raise ImportError("synthetic missing dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(formats, "_heif_available", None)
    monkeypatch.setattr(builtins, "__import__", missing)
    assert not formats.image_capabilities()["heif_decoder_available"]
    with pytest.raises(formats.ImageSourceError, match="IMG-HEIF-UNAVAILABLE"):
        formats.load_rgb(source)


def test_corrupt_heif_and_unsupported_dng_leave_no_cache(tmp_path):
    for suffix, code in [(".heic", "IMG-CORRUPT"), (".dng", "IMG-UNSUPPORTED")]:
        source = tmp_path / ("private" + suffix)
        source.write_bytes(b"invalid data")
        cache = ThumbnailCache(tmp_path / "cache")
        with pytest.raises(formats.ImageSourceError, match=code) as error:
            cache.get_or_create(source, sha256(source.read_bytes()).hexdigest(), 512)
        assert str(tmp_path) not in str(error.value)
        assert not list(cache.root.glob("*.tmp")) and not list(cache.root.glob("*.jpg"))


def test_file_edge_and_pixel_safety_before_decode(tmp_path):
    source = tmp_path / "huge.bmp"
    # Minimal BMP headers expose dimensions without allocating source pixels.
    for width, height in [(10000, 7000), (12001, 1)]:
        header = b"BM" + struct.pack("<IHHI", 54, 0, 0, 54)
        header += struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, 0, 0, 0, 0, 0)
        source.write_bytes(header)
        with pytest.raises(formats.ImageSourceError, match="THUMB-005"):
            formats.load_rgb(source, 512)
    with source.open("wb") as stream:
        stream.truncate(formats.MAX_FILE_BYTES + 1)
    with pytest.raises(formats.ImageSourceError, match="IMG-FILE-LIMIT"):
        formats.load_rgb(source, 512)
    assert Image.MAX_IMAGE_PIXELS is not None


@pytest.mark.parametrize("suffix", [".heic", ".jpg"])
def test_singleflight_atomic_heif_and_jpeg(tmp_path, monkeypatch, suffix):
    source = tmp_path / ("source" + suffix)
    digest = make_image(source)
    cache = ThumbnailCache(tmp_path / "cache")
    from inktime.app.domain.photos import thumbnails

    real = thumbnails.load_rgb
    calls = []

    def counted(*args):
        calls.append(1)
        return real(*args)

    monkeypatch.setattr(thumbnails, "load_rgb", counted)
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: cache.get_or_create(source, digest, 512), range(16)))
    assert len(set(paths)) == 1 and len(calls) == 1
    assert not list(cache.root.glob("*.tmp"))
    with Image.open(paths[0]) as image:
        image.load()
        assert image.format == "JPEG"


def test_shared_decode_slot_serializes_distinct_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("INKTIME_DATA_DIR", str(tmp_path))
    paths = [tmp_path / f"{index}.png" for index in range(4)]
    for path in paths:
        make_image(path)
    active = maximum = 0
    lock = threading.Lock()

    def load(path):
        nonlocal active, maximum
        with formats.safe_image_open(path) as opened:
            with lock:
                active += 1
                maximum = max(maximum, active)
            with formats.bounded_rgb(opened, 512):
                pass
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(load, paths))
    assert maximum == 1


def _hold_source_slot(path, entered, release):
    with formats.safe_image_open(Path(path)) as opened:
        entered.put("entered")
        assert release.wait(10)
        with formats.bounded_rgb(opened, 512):
            pass


def test_source_decode_slot_is_shared_by_spawned_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("INKTIME_DATA_DIR", str(tmp_path))
    paths = [tmp_path / "one.heic", tmp_path / "two.heic"]
    for path in paths:
        make_image(path)
    context = multiprocessing.get_context("spawn")
    entered = [context.Queue(), context.Queue()]
    releases = [context.Event(), context.Event()]
    processes = [
        context.Process(target=_hold_source_slot, args=(str(paths[i]), entered[i], releases[i]))
        for i in range(2)
    ]
    try:
        processes[0].start()
        assert entered[0].get(timeout=10) == "entered"
        processes[1].start()
        with pytest.raises(Empty):
            entered[1].get(timeout=0.5)
        releases[0].set()
        assert entered[1].get(timeout=10) == "entered"
        releases[1].set()
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
    finally:
        for release in releases:
            release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        for channel in entered:
            channel.close()


def test_source_change_permission_and_symlink_are_safe(tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    make_image(source)
    with pytest.raises(formats.ImageSourceError, match="THUMB-004"):
        with formats.safe_image_open(source) as opened:
            with formats.bounded_rgb(opened, 512):
                pass
            source.write_bytes(b"changed")
    link = tmp_path / "link.jpg"
    link.symlink_to(source)
    with pytest.raises(formats.ImageSourceError, match="IMG-IO"):
        formats.load_rgb(link)
    original = Path.lstat

    def denied(path):
        if path == source:
            raise PermissionError(errno.EACCES, "private path")
        return original(path)

    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(formats.ImageSourceError, match="IMG-IO") as error:
        formats.load_rgb(source)
    assert "private path" not in str(error.value)


def test_decode_lock_cannot_be_redirected_into_source_library(tmp_path, monkeypatch):
    monkeypatch.setenv("INKTIME_DATA_DIR", str(tmp_path))
    library = tmp_path / "library"
    library.mkdir()
    source = library / "photo.jpg"
    digest = make_image(source)
    (tmp_path / f".inktime-image-decode-{os.getuid()}").symlink_to(library, target_is_directory=True)
    with pytest.raises(formats.ImageSourceError, match="IMG-IO"):
        formats.load_rgb(source)
    assert list(library.iterdir()) == [source]
    assert sha256(source.read_bytes()).hexdigest() == digest


def test_cache_version_preserves_legacy_file_without_reusing_old_semantics(tmp_path):
    source = tmp_path / "transparent.png"
    Image.new("RGBA", (8, 8)).save(source)
    digest = sha256(source.read_bytes()).hexdigest()
    cache = ThumbnailCache(tmp_path / "cache")
    legacy = cache.root / f"{digest}-512.jpg"
    Image.new("RGB", (8, 8), "black").save(legacy)
    result = cache.get_or_create(source, digest, 512)
    assert result != legacy and legacy.exists()
    with Image.open(result) as image:
        assert image.getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize(
    "code,status",
    [
        ("IMG-CORRUPT", 422),
        ("IMG-UNSUPPORTED", 415),
        ("IMG-HEIF-UNAVAILABLE", 503),
        ("THUMB-004", 409),
        ("THUMB-005", 413),
        ("IMG-FILE-LIMIT", 413),
        ("IMG-MISSING", 404),
    ],
)
def test_permanent_image_failures_do_not_retry_without_source_or_config_fix(code, status):
    from inktime.app.domain.jobs.failure_policy import classify_failure, FailureClass

    error = formats.ImageSourceError(code, "synthetic", status)
    assert not error.retryable
    assert classify_failure(error) == FailureClass.TERMINAL_NO_RETRY
    assert classify_failure(code) == FailureClass.TERMINAL_NO_RETRY


def test_decode_capacity_failure_remains_retryable():
    from inktime.app.domain.jobs.failure_policy import classify_failure, FailureClass

    error = formats.ImageSourceError("IMG-DECODE-BUSY", "synthetic", 503)
    assert error.retryable
    assert classify_failure(error) == FailureClass.RETRYABLE
