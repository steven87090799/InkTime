from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

import inktime.app.db.migrations as migrations_module
from inktime.app.db import Database, migrate
from inktime.app.domain.rendering import encode_image
from inktime.app.domain.rendering import natural_d
from inktime.app.domain.rendering.release import AtomicReleasePublisher
from inktime.app.repositories.settings import SETTING_DEFINITIONS


FIXTURE = Path(__file__).parents[1] / "fixtures/natural_d_epdoptimize_1_3.json"


def _fixture_cases():
    return json.loads(FIXTURE.read_text())["cases"]


def _same_bytes(actual: bytes, expected: bytes) -> None:
    assert len(actual) == len(expected)
    mismatches = [(i, a, b) for i, (a, b) in enumerate(zip(actual, expected, strict=True)) if a != b]
    assert not mismatches, f"first reference byte differences: {mismatches[:8]}"


@pytest.mark.parametrize("sample", _fixture_cases(), ids=lambda sample: sample["name"])
def test_natural_d_matches_independent_epdoptimize_1_3_reference(sample):
    source = bytes.fromhex(sample["source_rgb_hex"])
    image = Image.frombytes("RGB", tuple(sample["size"]), source)
    balanced = natural_d._white_balance(np.frombuffer(source, dtype=np.uint8).reshape(-1, 3))
    _same_bytes(balanced.tobytes(), bytes.fromhex(sample["white_balanced_rgb_hex"]))
    prepared = natural_d._prepare(source, tuple(rgb for _, rgb in natural_d.SPECTRA6))
    _same_bytes(bytes(prepared), bytes.fromhex(sample["adjusted_rgb_hex"]))
    encoded = encode_image(image, profile_key="gdep073e01_6c", dither="natural_d")
    _same_bytes(encoded.payload, bytes.fromhex(sample["indexed4_hex"]))
    assert image.tobytes() == source
    assert {color.name: color.rgb for color in encoded.palette} == dict(natural_d.SPECTRA6)


def test_natural_d_bounds_float_chunks_to_2048_pixels(monkeypatch):
    sample = _fixture_cases()[0]
    observed = []
    original = natural_d._white_balance

    def recording(values):
        observed.append(len(values))
        return original(values)

    monkeypatch.setattr(natural_d, "_white_balance", recording)
    image = Image.frombytes("RGB", tuple(sample["size"]), bytes.fromhex(sample["source_rgb_hex"]))
    encode_image(image, profile_key="gdep073e01_6c", dither="natural_d")
    assert observed == [2048, 227]


def test_natural_d_rejects_oversized_input_before_rgb_conversion(monkeypatch):
    image = Image.new("L", (801, 480))

    def unexpected_conversion(*args, **kwargs):
        pytest.fail("oversized original must not allocate an RGB copy")

    monkeypatch.setattr(Image.Image, "convert", unexpected_conversion)
    with pytest.raises(ValueError, match="480×800"):
        encode_image(image, profile_key="gdep073e01_6c", dither="natural_d")


def test_natural_d_keeps_caption_mask_black_and_white():
    image = Image.new("RGB", (16, 12), (236, 224, 193))
    mask = Image.new("1", image.size)
    for y in range(8, 12):
        for x in range(16):
            image.putpixel((x, y), (255, 255, 255) if x % 2 else (0, 0, 0))
            mask.putpixel((x, y), 1)
    encoded = encode_image(image, profile_key="gdep073e01_6c", dither="natural_d", protected_mask=mask)
    for y in range(8, 12):
        for x in range(16):
            assert encoded.preview.getpixel((x, y)) == ((190, 200, 200) if x % 2 else (2, 2, 2))
    with pytest.raises(ValueError, match="遮罩尺寸"):
        encode_image(image, profile_key="gdep073e01_6c", dither="natural_d", protected_mask=Image.new("1", (1, 1)))


@pytest.mark.parametrize("profile,allowed,size", [("safe_4c", range(4), 8), ("gdey073d46_7c", range(7), 16)])
def test_natural_d_retains_other_panel_native_codes(profile, allowed, size):
    encoded = encode_image(Image.new("RGB", (4, 8), (225, 177, 133)), profile_key=profile, dither="natural_d")
    assert len(encoded.payload) == size
    if profile == "safe_4c":
        assert all((value >> shift) & 3 in allowed for value in encoded.payload for shift in (6, 4, 2, 0))
    else:
        assert all((value >> shift) & 15 in allowed for value in encoded.payload for shift in (4, 0))


def test_natural_d_release_records_actual_calibration_and_fixed_recipe(tmp_path):
    publisher = AtomicReleasePublisher(tmp_path / "releases")
    manifest = publisher.publish(
        [("synthetic-photo", Image.new("RGB", (8, 8), (225, 177, 133)))],
        profile_key="gdep073e01_6c", dither="natural_d", width=8, height=8,
        dither_strength=0, color_distance="oklab", activate=False,
    )
    assert manifest["dither_strength"] == 1
    assert manifest["color_distance"] == "rgb"
    assert manifest["palette_version"] == natural_d.VERSION
    assert manifest["render_options"]["color_pipeline"]["saturation_adjustment"] == -0.05
    assert {c["name"]: tuple(c["rgb"]) for c in manifest["palette"]} == dict(natural_d.SPECTRA6)
    publisher.validate(manifest["release_id"])


@pytest.mark.parametrize("value,actor,expected", [
    ("warm_vivid_c", None, "natural_d"),
    ("warm_vivid_c", "operator", "warm_vivid_c"),
    ("gooddisplay", None, "gooddisplay"),
    ("atkinson", "operator", "atkinson"),
])
def test_migration_75_promotes_only_untouched_default(monkeypatch, tmp_path, value, actor, expected):
    all_migrations = migrations_module.MIGRATIONS
    monkeypatch.setattr(migrations_module, "MIGRATIONS", all_migrations[:74])
    database = Database(tmp_path / "upgrade.db")
    migrate(database)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO settings(key,category,value_json,value_type,requires_restart,updated_by,updated_at) "
            "VALUES ('render.dither','渲染設定',?,'string',0,?,datetime('now'))",
            (json.dumps(value), actor),
        )
    monkeypatch.setattr(migrations_module, "MIGRATIONS", all_migrations)
    assert migrate(database) == [75]
    with database.session() as connection:
        row = connection.execute("SELECT value_json,updated_by FROM settings WHERE key='render.dither'").fetchone()
    assert json.loads(row["value_json"]) == expected
    assert row["updated_by"] == actor
    assert migrate(database) == []


def test_settings_expose_natural_d_and_keep_existing_modes():
    definition = SETTING_DEFINITIONS["render.dither"]
    assert definition["default"] == "natural_d"
    assert {"natural_d", "warm_vivid_c", "gooddisplay"} <= set(definition["choices"])
