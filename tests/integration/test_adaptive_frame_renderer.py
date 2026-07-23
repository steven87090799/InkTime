from __future__ import annotations

from pathlib import Path

from PIL import Image


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
        photo_id, None, "local", "local", "test",
        {"schema_version": 1, "caption": "測試", "types": ["人物"], "memory_score": 99,
         "beauty_score": 99, "technical_quality_score": 99, "emotion_score": 99,
         "side_caption": "這是一段足夠長的相框回憶短句，用來驗證 Footer 文字會被安全截斷。",
         "should_keep": True, "sensitive": False, "reason": "測試"}, "{}",
        ranking_score=99, final_ranking_score=99,
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


def test_device_releases_keep_profile_manifest_and_independent_layouts(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (900, 1600), "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "secondary", (900, 1600), "2024-07-01T10:30:00+00:00")
    devices = app.extensions["inktime_device_repository"]
    portrait_id, _ = devices.create("直向", frame_orientation="portrait", layout_mode="adaptive_memory")
    landscape_id, _ = devices.create("橫向", frame_orientation="landscape", layout_mode="adaptive_memory")
    result = app.extensions["inktime_render_service"].publish(
        ["primary"], "test", device_ids=[portrait_id, landscape_id]
    )
    assert set(result["device_releases"]) == {portrait_id, landscape_id}
    with app.extensions["inktime_database"].session() as connection:
        assignments = connection.execute(
            "SELECT device_id,release_id FROM device_render_releases ORDER BY device_id"
        ).fetchall()
    assert len(assignments) == 2
    for release_id in result["device_releases"].values():
        manifest = app.extensions["inktime_release_publisher"].validate(release_id)
        assert manifest["render_profile"] == "safe_4c"
        assert manifest["width"] == 480 and manifest["height"] == 800
        assert manifest["files"][0]["name"] == "photo_1.bin"
        assert manifest["files"][0]["size"] == 96_000
