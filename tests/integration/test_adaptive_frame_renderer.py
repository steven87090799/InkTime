from __future__ import annotations

from pathlib import Path

from PIL import Image

from inktime.app.services import rendering as rendering_service


def _analyzed_photo(app, root: Path, photo_id: str, size: tuple[int, int], captured_at: str):
    Image.new("RGB", size, "#4271a4").save(root / f"{photo_id}.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("自適應相框", root)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            INSERT INTO photos(id,library_id,relative_path,width,height,status,eligible,lifecycle_status,
                               captured_at,created_at,updated_at)
            VALUES (?,?,?,?,?,'analyzed',1,'active',?,?,?)
            """,
            (photo_id, library_id, f"{photo_id}.jpg", *size, captured_at, captured_at, captured_at),
        )
    photos.save_analysis(
        photo_id,
        None,
        "local",
        "local",
        "test",
        {
            "schema_version": 1,
            "caption": "測試",
            "types": ["人物"],
            "memory_score": 99,
            "beauty_score": 99,
            "technical_quality_score": 99,
            "emotion_score": 99,
            "side_caption": "這是一段足夠長的相框回憶短句，用來驗證 Footer 文字會被安全截斷。",
            "should_keep": True,
            "sensitive": False,
            "reason": "測試",
        },
        "{}",
        ranking_score=99,
        final_ranking_score=99,
    )


def _logical_landscape(image: Image.Image) -> Image.Image:
    return image.transpose(Image.Transpose.ROTATE_90)


def test_adaptive_landscape_single_contains_matching_photo_and_keeps_footer(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (1600, 900), "2024-07-01T10:00:00+00:00")
    image = app.extensions["inktime_render_service"].render_photo(
        "primary", layout="adaptive_memory", orientation="landscape"
    )
    logical = _logical_landscape(image)
    assert logical.size == (800, 480)
    assert logical.getpixel((10, 200)) == (255, 255, 255)
    assert logical.getpixel((400, 200)) != (255, 255, 255)
    assert logical.getpixel((10, 470)) == (255, 255, 255)


def test_adaptive_landscape_pair_contains_portraits_with_footer(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (900, 1600), "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "secondary", (900, 1600), "2024-07-01T10:30:00+00:00")
    image = app.extensions["inktime_render_service"].render_photo(
        "primary", layout="adaptive_memory", orientation="landscape"
    )
    logical = _logical_landscape(image)
    assert logical.getpixel((10, 100)) == (255, 255, 255)
    assert logical.getpixel((200, 200)) != (255, 255, 255)
    assert logical.getpixel((600, 200)) != (255, 255, 255)
    assert logical.getpixel((400, 470)) == (255, 255, 255)


def test_adaptive_portrait_single_contains_matching_photo_and_keeps_footer(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (900, 1600), "2024-07-01T10:00:00+00:00")
    image = app.extensions["inktime_render_service"].render_photo(
        "primary", layout="adaptive_memory", orientation="portrait"
    )
    assert image.getpixel((10, 300)) == (255, 255, 255)
    assert image.getpixel((240, 300)) != (255, 255, 255)
    assert image.getpixel((240, 780)) == (255, 255, 255)


def test_adaptive_portrait_pair_contains_landscapes_with_footer(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (1600, 900), "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "secondary", (1600, 900), "2024-07-01T10:30:00+00:00")
    image = app.extensions["inktime_render_service"].render_photo(
        "primary", layout="adaptive_memory", orientation="portrait"
    )
    assert image.getpixel((240, 10)) == (255, 255, 255)
    assert image.getpixel((240, 175)) != (255, 255, 255)
    assert image.getpixel((240, 530)) != (255, 255, 255)
    assert image.getpixel((240, 780)) == (255, 255, 255)


def test_adaptive_square_and_missing_pair_fall_back_to_single_contain(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "square", (1000, 1000), "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "portrait", (900, 1600), "2024-07-02T10:00:00+00:00")
    square = app.extensions["inktime_render_service"].render_photo(
        "square", layout="adaptive_memory", orientation="landscape"
    )
    fallback = app.extensions["inktime_render_service"].render_photo(
        "portrait", layout="adaptive_memory", orientation="landscape"
    )
    assert _logical_landscape(square).getpixel((10, 100)) == (255, 255, 255)
    assert _logical_landscape(fallback).getpixel((10, 100)) == (255, 255, 255)


def test_all_formal_frame_layouts_render_with_the_resolved_photos(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (1600, 900), "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "secondary", (900, 1600), "2024-07-01T10:30:00+00:00")
    service = app.extensions["inktime_render_service"]

    for layout in ("full", "postcard", "photo_info", "calendar", "weather_sensor"):
        image = service.render_photo("primary", layout=layout, orientation="landscape", fit_mode="cover")
        assert image.size == (480, 800)
    paired = service.render_photo(
        "primary",
        layout="photo_pair",
        secondary_photo_id="secondary",
        orientation="landscape",
        fit_mode="cover",
    )
    assert paired.size == (480, 800)


def test_formal_render_bounds_decoded_source_to_render_target(app, tmp_path, monkeypatch):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "large", (2400, 1600), "2024-07-01T10:00:00+00:00")
    service = app.extensions["inktime_render_service"]
    observed: list[tuple[int, int]] = []
    original_loader = service._load_oriented_photo

    def capture_loader(photo, path, *, target_size=None):
        source, orientation = original_loader(photo, path, target_size=target_size)
        observed.append(source.size)
        return source, orientation

    monkeypatch.setattr(service, "_load_oriented_photo", capture_loader)
    image = service.render_photo("large", layout="full", fit_mode="cover")

    assert image.size == (480, 800)
    assert observed
    assert observed[0][0] <= 960
    assert observed[0][1] <= 1600


def test_formal_render_accepts_48mp_jpeg_and_bounds_before_exif(app, tmp_path, monkeypatch):
    service = app.extensions["inktime_render_service"]
    before_exif: list[tuple[int, int]] = []

    class VirtualLargeJpeg:
        format = "JPEG"

        def __init__(self):
            self._image = Image.new("RGB", (120, 90), "#4271a4")
            self._reported_size = (8_000, 6_000)  # 48MP virtual JPEG fixture.
            self.draft_calls: list[tuple[str, tuple[int, int]]] = []

        @property
        def size(self):
            return self._reported_size

        @property
        def width(self):
            return self._reported_size[0]

        @property
        def height(self):
            return self._reported_size[1]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self._image.close()

        def draft(self, mode, size):
            self.draft_calls.append((mode, size))
            self._image = self._image.resize((1_200, 900), Image.Resampling.BOX)
            self._reported_size = self._image.size

        def thumbnail(self, size, resample):
            self._image.thumbnail(size, resample)
            self._reported_size = self._image.size

        def __getattr__(self, name):
            return getattr(self._image, name)

    virtual = VirtualLargeJpeg()
    original_exif_transpose = rendering_service.ImageOps.exif_transpose

    def open_virtual(_path):
        return virtual

    def observe_before_exif(image, *args, **kwargs):
        before_exif.append(image.size)
        return original_exif_transpose(image, *args, **kwargs)

    monkeypatch.setattr(rendering_service.Image, "open", open_virtual)
    monkeypatch.setattr(rendering_service.ImageOps, "exif_transpose", observe_before_exif)
    source, _orientation = service._load_oriented_photo(
        {}, tmp_path / "virtual-48mp.jpg", target_size=(480, 800)
    )
    try:
        assert virtual.width * virtual.height <= 60_000_000
        assert virtual.draft_calls == [("RGB", (960, 1_600))]
        assert before_exif == [(960, 720)]
        assert source.size == (960, 720)
    finally:
        source.close()


def test_device_releases_keep_profile_manifest_and_independent_layouts(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (900, 1600), "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "secondary", (900, 1600), "2024-07-01T10:30:00+00:00")
    devices = app.extensions["inktime_device_repository"]
    portrait_id, _ = devices.create("直向", frame_orientation="portrait", layout_mode="adaptive_memory")
    landscape_id, _ = devices.create(
        "橫向",
        panel_profile="gdep073e01_6c",
        frame_orientation="landscape",
        layout_mode="adaptive_memory",
    )
    result = app.extensions["inktime_render_service"].publish(
        ["primary"], "test", device_ids=[portrait_id, landscape_id]
    )
    assert set(result["device_releases"]) == {portrait_id, landscape_id}
    with app.extensions["inktime_database"].session() as connection:
        assignments = connection.execute(
            "SELECT device_id,release_id FROM device_render_releases ORDER BY device_id"
        ).fetchall()
    assert len(assignments) == 2
    secondary_ids = set()
    expected_profiles = {portrait_id: "safe_4c", landscape_id: "gdep073e01_6c"}
    for release_id in result["device_releases"].values():
        manifest = app.extensions["inktime_release_publisher"].validate(release_id)
        device_id = next(key for key, value in result["device_releases"].items() if value == release_id)
        assert manifest["render_profile"] == expected_profiles[device_id]
        assert manifest["width"] == 480 and manifest["height"] == 800
        assert manifest["files"][0]["name"] == "photo_1.bin"
        assert manifest["files"][0]["size"] == (
            96_000 if manifest["render_profile"] == "safe_4c" else 192_000
        )
        options = manifest["render_options"]
        assert options["aggregation_scope"] == "release"
        assert options["render_plans"][0]["primary_photo_id"] == "primary"
        secondary_ids.add(options["render_plans"][0]["secondary_photo_id"])
        assert "secondary_sha256" in options["render_plans"][0]
        assert options["effective_dither"] == manifest["dither"]
        assert options["render_plans"][0]["profile"] == manifest["render_profile"]
        assert options["render_plans"][0]["effective_dither"] == manifest["dither"]
    assert secondary_ids == {None, "secondary"}


def test_each_manifest_binds_the_release_dither_and_its_own_profile_plan(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "low-risk", (900, 1600), "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "high-risk", (1600, 900), "2024-07-01T10:30:00+00:00")
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.auto_photo_smooth_enabled", True, changed_by="test", source_ip="127.0.0.1")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            UPDATE photos
            SET brightness=40,contrast=5,underexposed_ratio=.7,e6_score=20,
                e6_contrast_score=20,e6_subject_score=20
            WHERE id='high-risk'
            """
        )

    published = app.extensions["inktime_render_service"].publish(
        ["low-risk", "high-risk"],
        "test",
        profile_keys=["safe_4c", "gdep073e01_6c"],
    )
    assert len(published["releases"]) == 2
    for item in published["releases"]:
        manifest = app.extensions["inktime_release_publisher"].validate(item["release_id"])
        options = manifest["render_options"]
        assert manifest["dither"] == "photo_smooth"
        assert options["effective_dither"] == manifest["dither"]
        assert options["quantization_plan"]["effective_dither"] == manifest["dither"]
        assert options["quantization_plan"]["profile_key"] == manifest["render_profile"]
        assert {plan["profile"] for plan in options["render_plans"]} == {manifest["render_profile"]}
        assert {plan["effective_dither"] for plan in options["render_plans"]} == {manifest["dither"]}
        assert all(plan["aggregation_scope"] == "release" for plan in options["render_plans"])
        assert any(
            risk["photo_id"] == "high-risk" and risk["risk"] == "high"
            for risk in options["quantization_plan"]["photo_risks"]
        )


def test_adaptive_secondary_risk_controls_the_preview_dither_plan(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (900, 1600), "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "secondary", (900, 1600), "2024-07-01T10:30:00+00:00")
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.auto_photo_smooth_enabled", True, changed_by="test", source_ip="127.0.0.1")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            UPDATE photos
            SET brightness=40,contrast=5,underexposed_ratio=.7,e6_score=20,
                e6_contrast_score=20,e6_subject_score=20
            WHERE id='secondary'
            """
        )

    service = app.extensions["inktime_render_service"]
    fingerprint = service.preview_fingerprint("primary", layout="adaptive_memory", orientation="landscape")
    plan = fingerprint["render_plan"]

    assert plan["secondary_photo_id"] == "secondary"
    assert plan["effective_dither"] == "photo_smooth"
    assert plan["override_source"] == "auto_photo_smooth"
    assert fingerprint["render_settings"]["effective_dither"] == "photo_smooth"
    assert any(risk == {"photo_id": "secondary", "risk": "high"} for risk in plan["photo_risks"])
