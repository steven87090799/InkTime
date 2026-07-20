from __future__ import annotations

from PIL import Image

from inktime.app.domain.rendering import DITHER_ALGORITHMS, encode_image, get_display_profile


GOODDISPLAY_PALETTE = (
    (0, 0, 0),
    (255, 255, 255),
    (255, 255, 0),
    (255, 0, 0),
    (0, 0, 0),
    (0, 0, 255),
    (0, 255, 0),
)
GOODDISPLAY_CODE_BY_RGB = {
    (0, 0, 0): 0,
    (255, 255, 255): 1,
    (0, 255, 0): 2,
    (0, 0, 255): 3,
    (255, 0, 0): 4,
    (255, 255, 0): 5,
}


def _unpack_indexed4(payload: bytes, pixels: int) -> list[int]:
    return [
        payload[index // 2] >> 4 if index % 2 == 0 else payload[index // 2] & 0x0F
        for index in range(pixels)
    ]


def _solid_blocks(image: Image.Image, size: int = 4) -> int:
    pixels = image.load()
    return sum(
        all(
            pixels[column, row] == pixels[x, y]
            for row in range(y, y + size)
            for column in range(x, x + size)
        )
        for y in range(0, image.height - size + 1, size)
        for x in range(0, image.width - size + 1, size)
    )


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

    for algorithm in DITHER_ALGORITHMS:
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


def test_gooddisplay_mode_matches_the_vendor_fixed_palette_converter():
    image = Image.new("RGB", (16, 12))
    for y in range(image.height):
        for x in range(image.width):
            image.putpixel((x, y), (x * 17, y * 23, (x + y) * 9))

    palette = Image.new("P", (1, 1))
    flattened = tuple(channel for color in GOODDISPLAY_PALETTE for channel in color)
    palette.putpalette(flattened + (0, 0, 0) * (256 - len(GOODDISPLAY_PALETTE)))
    reference = image.quantize(
        palette=palette,
        dither=Image.Dither.FLOYDSTEINBERG,
    ).convert("RGB")
    encoded = encode_image(image, profile_key="gdep073e01_6c", dither="gooddisplay")

    assert list(encoded.preview.getdata()) == list(reference.getdata())
    assert _unpack_indexed4(encoded.payload, image.width * image.height) == [
        GOODDISPLAY_CODE_BY_RGB[pixel] for pixel in reference.getdata()
    ]
    assert [color.rgb for color in encoded.palette] == [
        (0, 0, 0),
        (255, 255, 255),
        (0, 255, 0),
        (0, 0, 255),
        (255, 0, 0),
        (255, 255, 0),
    ]


def test_photo_smooth_reduces_large_same_color_blocks():
    image = Image.new("RGB", (96, 96))
    for y in range(image.height):
        for x in range(image.width):
            image.putpixel((x, y), (x * 255 // 95, y * 255 // 95, (x + y) * 255 // 190))

    existing = encode_image(
        image,
        profile_key="gdep073e01_6c",
        dither="floyd_steinberg",
        color_distance="oklab",
    )
    optimized = encode_image(
        image,
        profile_key="gdep073e01_6c",
        dither="photo_smooth",
        color_distance="oklab",
    )

    assert _solid_blocks(optimized.preview) < _solid_blocks(existing.preview)
    assert len(optimized.payload) == len(existing.payload)
