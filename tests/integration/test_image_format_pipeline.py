import base64
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest

from inktime.app.domain.photos import PhotoPreprocessor
from inktime.app.domain.photos import formats
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider
from inktime.app.workers.scanner import PhotoScanner
from inktime.app.services.analysis import PhotoAnalysisService
from inktime.app.repositories.usage import UsageRepository
from tests.conftest import create_admin, login
from tests.integration.test_analysis_pipeline import MockProvider
from tests.unit.test_analysis_schema import valid_result
from tests.unit.test_image_format_contract import make_image


def scan(app, root, **kwargs):
    return PhotoScanner(
        app.extensions["inktime_photo_repository"],
        PhotoPreprocessor(),
        app.extensions["inktime_thumbnail_cache"],
    ).scan("Synthetic", root, **kwargs)


def photo_rows(app):
    with app.extensions["inktime_database"].session() as connection:
        return list(connection.execute("SELECT * FROM photos ORDER BY relative_path"))


class JpegOnlyProvider(MockProvider):
    def analyze(self, **kwargs):
        image_path = Path(kwargs["image_path"])
        assert image_path.suffix == ".jpg"
        with Image.open(image_path) as image:
            image.load()
            assert image.format == "JPEG" and image.mode == "RGB"
            assert max(image.size) <= 1024 and image.getexif().get(274) is None
        # Exercise the actual wire payload builder without network or API keys.
        with_provider = OpenAICompatibleProvider(
            name="offline", base_url="https://example.invalid/v1", api_key=""
        )
        try:
            body = with_provider.build_analysis_request_body(
                image_path=image_path, model="offline", stage="high", detail="high"
            )
        finally:
            with_provider.close()
        url = body["messages"][1]["content"][1]["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == image_path.read_bytes()
        return super().analyze(**kwargs)


@pytest.mark.parametrize("suffix", [".heic", ".heif", ".jpg", ".png", ".webp", ".tiff", ".bmp"])
def test_source_to_browser_review_ai_render_and_release(app, client, tmp_path, suffix, monkeypatch):
    import requests

    monkeypatch.setattr(requests.Session, "request", lambda *args, **kwargs: pytest.fail("network forbidden"))
    root = tmp_path / "library"
    root.mkdir()
    source = root / ("source" + suffix)
    original_hash = make_image(source, orientation=6)
    source.chmod(0o444)
    result = scan(app, root)
    assert result["processed"] == 1 and result["failed"] == 0
    photo = photo_rows(app)[0]
    photo_id = photo["id"]
    admin = create_admin(app)
    login(client)
    for endpoint, size in [
        (f"/api/v1/photos/{photo_id}/thumbnail", 512),
        (f"/api/v1/photos/{photo_id}/preview", 1600),
        (f"/api/v1/review/photos/{photo_id}/thumbnail", 512),
    ]:
        response = client.get(endpoint)
        assert response.status_code == 200
        assert response.mimetype == "image/jpeg"
        with Image.open(BytesIO(response.data)) as image:
            assert image.format == "JPEG" and image.mode == "RGB"
            assert max(image.size) <= size
            assert image.size == (photo["width"], photo["height"])
            assert image.getexif().get(274) is None
    detail = client.get(f"/photos/{photo_id}")
    assert detail.status_code == 200
    assert f"/api/v1/photos/{photo_id}/preview".encode() in detail.data
    assert f"/api/v1/photos/{photo_id}/image".encode() not in detail.data
    assert "縮圖無法產生".encode() in client.get("/photos").data
    assert "review-image-error" in client.get("/review/photos").text
    # The original API is retained for existing consumers, without UI img use.
    assert client.get(f"/api/v1/photos/{photo_id}/image").data == source.read_bytes()
    provider = JpegOnlyProvider([valid_result()])
    service = PhotoAnalysisService(
        app.extensions["inktime_photo_repository"],
        UsageRepository(app.extensions["inktime_database"]),
        app.extensions["inktime_thumbnail_cache"],
    )
    analysis = service.analyze_photo(
        photo_id=photo_id, job_id=None, provider=provider, strategy="high_quality", high_model="offline"
    )
    assert analysis["analysis"]["side_caption"] and provider.analyze_calls == 1
    derivative = Path(provider.analyze_kwargs[0]["image_path"])
    assert derivative.read_bytes() != source.read_bytes()
    renderer = app.extensions["inktime_render_service"]
    refreshed = app.extensions["inktime_photo_repository"].get_with_path(photo_id)
    oriented, _ = renderer._load_oriented_photo(refreshed, source, target_size=(480, 800))
    assert oriented.size == (photo["width"], photo["height"])
    oriented.close()
    # The deliberately tiny color chart is excluded by normal quality rules.
    # Explicitly restore this synthetic fixture through the audited repository
    # API; this tests format transport, not photographic quality selection.
    app.extensions["inktime_photo_repository"].set_exclusion(photo_id, action="restore", changed_by=admin)
    # Formal E6 render/release on disposable test storage; no device is assigned.
    release = renderer.publish(
        [photo_id],
        admin,
        profile_keys=["gdep073e01_6c"],
        activate_pointers=False,
        assign_device_releases=False,
    )
    assert release
    assert sha256(source.read_bytes()).hexdigest() == original_hash


def test_48mp_jpeg_shared_acceptance_and_draft_decode(app, tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    source = root / "48mp.jpg"
    # A grayscale source requires only 48 MB to encode, with no huge RGB fixture.
    with Image.new("L", (8000, 6000), 128) as image:
        image.save(source, quality=30)
    from PIL import JpegImagePlugin

    real_load = JpegImagePlugin.JpegImageFile.load
    decoded_sizes = []

    def tracked_load(image, *args, **kwargs):
        if image.width > 512:
            decoded_sizes.append(image.size)
        return real_load(image, *args, **kwargs)

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "load", tracked_load)
    result = scan(app, root)
    assert result["processed"] == 1 and result["failed"] == 0
    row = photo_rows(app)[0]
    assert (row["width"], row["height"]) == (8000, 6000)
    cache = app.extensions["inktime_thumbnail_cache"]
    for size in (512, 1024, 1600):
        with Image.open(cache.get_or_create(source, row["sha256"], size)) as image:
            assert image.format == "JPEG" and max(image.size) <= size
    assert decoded_sizes and max(w * h for w, h in decoded_sizes) <= 12_000_000
    assert formats.MAX_SOURCE_PIXELS >= 48_000_000


def test_real_48mp_rgb_heif_all_derivative_consumers(app, client, tmp_path, monkeypatch):
    import requests

    monkeypatch.setattr(requests.Session, "request", lambda *args, **kwargs: pytest.fail("network forbidden"))
    root = tmp_path / "library"
    root.mkdir()
    source = root / "48mp.heic"
    assert formats.ensure_image_codecs_registered()
    # Actual 48.8MP RGB HEIF grid, generated from public synthetic pixels.
    # One fixture, one encoder thread, no parallel huge allocations in CI.
    with Image.new("RGB", (8064, 6048), (80, 120, 160)) as image:
        image.save(
            source,
            quality=10,
            tile_size=512,
            enc_params={"preset": "ultrafast", "x265:pools": "1", "x265:frame-threads": "1"},
        )
    original_hash = sha256(source.read_bytes()).hexdigest()
    source.chmod(0o444)
    result = scan(app, root)
    assert result["processed"] == 1 and result["failed"] == 0
    row = photo_rows(app)[0]
    assert (row["width"], row["height"], row["format"]) == (8064, 6048, "HEIF")
    create_admin(app)
    login(client)
    response = client.get(f"/api/v1/photos/{row['id']}/preview")
    assert response.status_code == 200 and response.mimetype == "image/jpeg"
    with Image.open(BytesIO(response.data)) as preview:
        assert preview.size == (1600, 1200)
    provider = JpegOnlyProvider([valid_result()])
    service = PhotoAnalysisService(
        app.extensions["inktime_photo_repository"],
        UsageRepository(app.extensions["inktime_database"]),
        app.extensions["inktime_thumbnail_cache"],
    )
    service.analyze_photo(
        photo_id=row["id"], job_id=None, provider=provider, strategy="high_quality", high_model="offline"
    )
    assert provider.analyze_calls == 1
    renderer = app.extensions["inktime_render_service"]
    image, _ = renderer._load_oriented_photo(row, source, target_size=(480, 800))
    assert max(image.size) <= 1600 and image.mode == "RGB"
    image.close()
    assert sha256(source.read_bytes()).hexdigest() == original_hash


def test_scanner_safety_limit_cannot_be_overridden(app, tmp_path):
    import struct

    root = tmp_path / "library"
    root.mkdir()
    source = root / "giant.bmp"
    source.write_bytes(
        b"BM"
        + struct.pack("<IHHI", 54, 0, 0, 54)
        + struct.pack("<IiiHHIIiiII", 40, 10000, 7000, 1, 24, 0, 0, 0, 0, 0, 0)
    )
    result = scan(app, root, max_pixels=500_000_000)
    assert result["failed"] == 1 and result["processed"] == 0
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT error_code FROM scan_errors").fetchone()[0] == "THUMB-005"


def test_live_photo_raw_and_corrupt_accounting(app, tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    make_image(root / "live.heic")
    (root / "live.mov").write_bytes(b"video")
    (root / "raw.dng").write_bytes(b"raw")
    (root / "bad.heic").write_bytes(b"bad")
    result = scan(app, root)
    assert result["processed"] == 1 and result["failed"] == 1
    assert result["excluded_videos"] == 1 and result["unsupported_raw"] == 1
    assert len(photo_rows(app)) == 1
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT error_code FROM scan_errors").fetchone()[0] == "IMG-CORRUPT"


@pytest.mark.parametrize(
    "case,status,code",
    [
        ("missing", 404, "IMG-MISSING"),
        ("corrupt", 422, "IMG-CORRUPT"),
        ("decoder", 503, "IMG-HEIF-UNAVAILABLE"),
        ("changed", 409, "THUMB-004"),
        ("huge_file", 413, "IMG-FILE-LIMIT"),
    ],
)
def test_preview_errors_are_structured_private_and_atomic(
    app, client, tmp_path, monkeypatch, case, status, code
):
    root = tmp_path / "private-library"
    root.mkdir()
    source = root / "private-family.heic"
    make_image(source)
    scan(app, root, build_thumbnails=False)
    photo_id = photo_rows(app)[0]["id"]
    create_admin(app)
    login(client)
    if case == "missing":
        source.unlink()
    elif case == "corrupt":
        source.write_bytes(b"corrupt")
    elif case == "decoder":
        monkeypatch.setattr(formats, "_heif_available", False)
        assert client.get("/health/ready").json["checks"]["heif_decoder"] is False
    elif case == "changed":
        make_image(source, size=(80, 120))
    elif case == "huge_file":
        with source.open("wb") as stream:
            stream.truncate(formats.MAX_FILE_BYTES + 1)
    response = client.get(f"/api/v1/photos/{photo_id}/preview")
    assert response.status_code == status and response.json["error_code"] == code
    assert str(root) not in response.text and "private-family" not in response.text
    cache = app.extensions["inktime_thumbnail_cache"]
    assert not list(cache.root.glob("*.tmp")) and not list(cache.root.glob("*.jpg"))


def test_provider_refuses_raw_heif_before_reading_bytes(tmp_path):
    provider = OpenAICompatibleProvider(name="offline", base_url="https://example.invalid/v1", api_key="")
    try:
        with pytest.raises(ValueError, match="AI-IMAGE-001"):
            provider.build_analysis_request_body(
                image_path=tmp_path / "never-opened.heic", model="offline", detail="high", stage="high"
            )
    finally:
        provider.close()


@pytest.mark.parametrize("cached_size", [None, 512, 1600])
def test_missing_original_keeps_history_and_serves_retained_preview(app, client, tmp_path, cached_size):
    root = tmp_path / "library"
    root.mkdir()
    source = root / "retained.jpg"
    make_image(source)
    scan(app, root, build_thumbnails=False)
    photo = photo_rows(app)[0]
    cache = app.extensions["inktime_thumbnail_cache"]
    expected = None
    if cached_size:
        expected = cache.get_or_create(source, photo["sha256"], cached_size).read_bytes()
    source.unlink()
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET lifecycle_status='missing' WHERE id=?", (photo["id"],))
    create_admin(app)
    login(client)
    response = client.get(f"/api/v1/photos/{photo['id']}/preview")
    if expected:
        assert response.status_code == 200 and response.data == expected
        assert response.headers["X-InkTime-Photo-Source"] == "retained-preview"
    else:
        assert response.status_code == 404
        assert response.json["error_code"] == "IMG-MISSING"
    detail = client.get(f"/photos/{photo['id']}")
    assert detail.status_code == 200
    assert "來源原檔無法使用" in detail.text
    assert "符合候選資格" not in detail.text
    assert ("目前只剩保留縮圖" if expected else "也沒有可用的保留縮圖") in detail.text
    assert len(photo_rows(app)) == 1
