from __future__ import annotations

from hashlib import sha256
import json

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
