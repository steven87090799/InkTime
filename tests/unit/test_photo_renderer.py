from __future__ import annotations

from PIL import Image, ImageDraw

from inktime.app.domain.rendering import encode_image, get_display_profile, palette_for_profile, render_photo


def _person_photo() -> Image.Image:
    image = Image.new("RGB", (180, 260), "#d8c0a8")
    draw = ImageDraw.Draw(image)
    draw.ellipse((45, 30, 135, 125), fill="#d79c7b")
    draw.rounded_rectangle((28, 118, 152, 255), 28, fill="#315f91")
    return image


def _landscape_photo() -> Image.Image:
    image = Image.new("RGB", (320, 180), "#79a9d1")
    draw = ImageDraw.Draw(image)
    draw.polygon(((0, 150), (90, 60), (170, 145), (245, 75), (320, 150)), fill="#39714f")
    draw.rectangle((0, 145, 320, 180), fill="#dfb647")
    return image


def _text_layout() -> Image.Image:
    image = Image.new("RGB", (480, 800), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 40, 460, 760), outline="black", width=8)
    for y in range(100, 700, 70):
        draw.rectangle((55, y, 425, y + 18), fill="black")
    return image


def test_person_landscape_and_text_presets_produce_six_color_indexed4():
    cases = (
        (_person_photo(), "portrait_clear"),
        (_landscape_photo(), "landscape_smooth"),
        (_text_layout(), "text_graphic"),
    )
    for image, preset in cases:
        result = render_photo(
            image,
            profile_key="gdep073e01_6c",
            preset=preset,
            overrides={"dither": "nearest"},
            text_regions=[[0, 0, 1, 1]] if preset == "text_graphic" else [],
        )
        assert result.encoded.preview.size == (480, 800)
        assert len(result.encoded.payload) == 192_000


def test_serpentine_floyd_steinberg_is_deterministic_in_linear_light():
    image = Image.new("RGB", (32, 24))
    for y in range(image.height):
        for x in range(image.width):
            image.putpixel((x, y), (x * 7, y * 10, (x + y) * 4))
    first = encode_image(
        image,
        profile_key="gdep073e01_6c",
        dither="serpentine_floyd_steinberg",
        linear_light=True,
        strength=0.85,
    )
    second = encode_image(
        image,
        profile_key="gdep073e01_6c",
        dither="serpentine_floyd_steinberg",
        linear_light=True,
        strength=0.85,
    )
    assert first.payload == second.payload
    assert len(first.payload) == image.width * image.height // 2


def test_custom_lab_palette_is_request_local_and_preserves_theoretical_rgb():
    built_in = get_display_profile("gdep073e01_6c")
    custom = palette_for_profile(
        "gdep073e01_6c",
        lab_values={"red": [52.0, 72.0, 48.0]},
        palette_version="panel-sample-1",
    )
    assert custom.palette_version == "panel-sample-1"
    assert custom.colors[4].lab == (52.0, 72.0, 48.0)
    assert get_display_profile("gdep073e01_6c") is built_in
    assert built_in.colors[4].rgb == (210, 35, 35)
