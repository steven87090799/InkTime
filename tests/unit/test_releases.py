from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from datetime import date

from PIL import Image
import pytest

from inktime.app.domain.rendering.fonts import FontCoverageError
from inktime.app.domain.rendering.palette import encode_image
from inktime.app.domain.rendering.release import AtomicReleasePublisher, pack_four_color_2bpp


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


def test_automatic_release_candidates_respect_configured_memory_threshold(app, tmp_path):
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
            INSERT INTO photos(id,library_id,relative_path,status,captured_at,e6_score,created_at,updated_at)
            VALUES (?,?,?,'analyzed',?,80,?,?)
            """,
            [(photo_id, library_id, path, captured, now, now) for photo_id, path, captured, _ in entries],
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
