from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from PIL import Image
import pytest

from inktime.app.domain.rendering.fonts import FontCoverageError
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

    second = publisher.publish([("photo-2", Image.new("RGB", (480, 800), "black"))])
    publisher.rollback(first["release_id"])
    assert (tmp_path / "releases" / "latest").read_text() == first["release_id"]
    assert second["release_id"] != first["release_id"]


def test_failed_release_does_not_replace_latest(tmp_path):
    publisher = AtomicReleasePublisher(tmp_path / "releases")
    first = publisher.publish([("photo-1", Image.new("RGB", (480, 800), "white"))])
    with pytest.raises(ValueError):
        publisher.publish([("broken", Image.new("RGB", (100, 100), "white"))])
    assert (tmp_path / "releases" / "latest").read_text() == first["release_id"]


def test_automatic_release_candidates_respect_configured_memory_threshold(app, tmp_path):
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("測試照片", Path(tmp_path / "photos"))
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
