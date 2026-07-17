from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from PIL import Image
import pytest

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
