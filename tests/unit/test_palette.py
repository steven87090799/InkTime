from __future__ import annotations

from PIL import Image

from inktime.app.domain.rendering import encode_image, get_display_profile


def test_six_and_seven_color_profiles_are_packed_as_indexed4():
    image = Image.new("RGB", (480, 800), (240, 110, 20))
    six = encode_image(image, profile_key="gdep073e01_6c", dither="none")
    seven = encode_image(image, profile_key="gdey073d46_7c", dither="none")

    assert len(six.payload) == 192_000
    assert len(seven.payload) == 192_000
    assert max(value >> 4 for value in six.payload) <= 5
    assert set(seven.payload) == {0x66}
    assert get_display_profile("gdey073d46_7c").colors[6].name == "orange"


def test_every_dither_algorithm_is_deterministic_and_uses_only_profile_codes():
    image = Image.new("RGB", (8, 8))
    for y in range(8):
        for x in range(8):
            image.putpixel((x, y), (x * 31, y * 31, (x + y) * 15))

    for algorithm in ("none", "floyd_steinberg", "atkinson", "bayer4", "bayer8"):
        first = encode_image(
            image,
            profile_key="gdey073d46_7c",
            dither=algorithm,
            color_distance="oklab",
        )
        second = encode_image(
            image,
            profile_key="gdey073d46_7c",
            dither=algorithm,
            color_distance="oklab",
        )
        assert first.payload == second.payload
        codes = {value >> 4 for value in first.payload} | {value & 0x0F for value in first.payload}
        assert codes <= set(range(7))
