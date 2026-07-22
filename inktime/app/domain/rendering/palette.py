from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any, Mapping, Sequence, cast

from PIL import Image, ImageFilter


@dataclass(frozen=True)
class PaletteColor:
    code: int
    name: str
    rgb: tuple[int, int, int]
    lab: tuple[float, float, float] | None = None


@dataclass(frozen=True)
class DisplayProfile:
    key: str
    label: str
    display_type: str
    pixel_format: str
    colors: tuple[PaletteColor, ...]
    panel_profile: str = "generic"
    palette_version: str = "1"

    @property
    def bytes_per_image(self) -> int:
        pixels = 480 * 800
        return pixels // 4 if self.pixel_format == "2bpp" else pixels // 2


@dataclass(frozen=True)
class EncodedImage:
    payload: bytes
    preview: Image.Image
    palette: tuple[PaletteColor, ...]


DISPLAY_PROFILES: dict[str, DisplayProfile] = {
    "safe_4c": DisplayProfile(
        key="safe_4c",
        label="通用四色（黑／白／紅／黃）",
        display_type="7.3-inch-four-color",
        pixel_format="2bpp",
        colors=(
            PaletteColor(0, "black", (0, 0, 0)),
            PaletteColor(1, "white", (255, 255, 255)),
            PaletteColor(2, "red", (220, 30, 30)),
            PaletteColor(3, "yellow", (245, 190, 25)),
        ),
        panel_profile="generic-4c",
        palette_version="legacy-1",
    ),
    "gdep073e01_6c": DisplayProfile(
        key="gdep073e01_6c",
        label="GDEP073E01 Spectra 6（六色）",
        display_type="GoodDisplay-GDEP073E01-Spectra6",
        pixel_format="indexed4",
        colors=(
            PaletteColor(0, "black", (0, 0, 0)),
            PaletteColor(1, "white", (255, 255, 255)),
            PaletteColor(2, "green", (25, 155, 70)),
            PaletteColor(3, "blue", (30, 85, 170)),
            PaletteColor(4, "red", (210, 35, 35)),
            PaletteColor(5, "yellow", (240, 195, 25)),
        ),
        panel_profile="waveshare-gdep073e01",
        palette_version="waveshare-theoretical-1",
    ),
    "gdey073d46_7c": DisplayProfile(
        key="gdey073d46_7c",
        label="GDEY073D46 ACeP（七色）",
        display_type="GoodDisplay-GDEY073D46-ACeP7",
        pixel_format="indexed4",
        colors=(
            PaletteColor(0, "black", (0, 0, 0)),
            PaletteColor(1, "white", (255, 255, 255)),
            PaletteColor(2, "green", (25, 155, 70)),
            PaletteColor(3, "blue", (30, 85, 170)),
            PaletteColor(4, "red", (210, 35, 35)),
            PaletteColor(5, "yellow", (240, 195, 25)),
            PaletteColor(6, "orange", (240, 110, 20)),
        ),
        panel_profile="waveshare-gdey073d46",
        palette_version="waveshare-theoretical-1",
    ),
}

DITHER_ALGORITHMS = (
    "none",
    "floyd_steinberg",
    "gooddisplay",
    "photo_smooth",
    "atkinson",
    "bayer4",
    "bayer8",
    "nearest",
    "bayer_ordered",
    "serpentine_floyd_steinberg",
)
COLOR_DISTANCES = ("oklab", "rgb")

_GOODDISPLAY_GDEP073E01_PALETTE = (
    (0, 0, 0),
    (255, 255, 255),
    (255, 255, 0),
    (255, 0, 0),
    (0, 0, 0),
    (0, 0, 255),
    (0, 255, 0),
)
_GOODDISPLAY_GDEP073E01_TO_PROFILE = (0, 1, 5, 4, 0, 3, 2)
_GOODDISPLAY_RGB_BY_NAME = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "red": (255, 0, 0),
    "yellow": (255, 255, 0),
}


def get_display_profile(key: str) -> DisplayProfile:
    try:
        return DISPLAY_PROFILES[key]
    except KeyError as exc:
        raise ValueError(f"RENDER-003 不支援的顯示 Profile：{key}") from exc


def profile_summaries() -> list[dict]:
    return [
        {
            "key": profile.key,
            "label": profile.label,
            "display_type": profile.display_type,
            "pixel_format": profile.pixel_format,
            "bytes_per_image": profile.bytes_per_image,
            "panel_profile": profile.panel_profile,
            "palette_version": profile.palette_version,
            "colors": [
                {"code": color.code, "name": color.name, "rgb": list(color.rgb)}
                for color in profile.colors
            ],
            **{
                f"{name}_lab": list(_color_lab(profile, name))
                for name in ("black", "white", "red", "yellow", "blue", "green")
                if any(color.name == name for color in profile.colors)
            },
            "gooddisplay_colors": [
                {"code": color.code, "name": color.name, "rgb": list(color.rgb)}
                for color in _gooddisplay_colors(profile)
            ],
        }
        for profile in DISPLAY_PROFILES.values()
    ]


def _gooddisplay_colors(profile: DisplayProfile) -> tuple[PaletteColor, ...]:
    if profile.key != "gdep073e01_6c":
        return profile.colors
    return tuple(
        PaletteColor(color.code, color.name, _GOODDISPLAY_RGB_BY_NAME[color.name])
        for color in profile.colors
    )


def _linear_channel(value: float) -> float:
    value /= 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _srgb_channel(value: float) -> float:
    value = max(0.0, min(1.0, value))
    encoded = 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055
    return encoded * 255.0


def _oklab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    red, green, blue = (_linear_channel(float(value)) for value in rgb)
    light = 0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue
    medium = 0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue
    short = 0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue
    light, medium, short = (value ** (1.0 / 3.0) for value in (light, medium, short))
    return (
        0.2104542553 * light + 0.7936177850 * medium - 0.0040720468 * short,
        1.9779984951 * light - 2.4285922050 * medium + 0.4505937099 * short,
        0.0259040371 * light + 0.7827717662 * medium - 0.8086757660 * short,
    )


def _lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    red, green, blue = (_linear_channel(float(value)) for value in rgb)
    x = (0.4124564 * red + 0.3575761 * green + 0.1804375 * blue) / 0.95047
    y = 0.2126729 * red + 0.7151522 * green + 0.0721750 * blue
    z = (0.0193339 * red + 0.1191920 * green + 0.9503041 * blue) / 1.08883

    def pivot(value: float) -> float:
        return value ** (1 / 3) if value > 0.008856 else 7.787 * value + 16 / 116

    fx, fy, fz = pivot(x), pivot(y), pivot(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _lab_to_rgb(lab: tuple[float, float, float]) -> tuple[int, int, int]:
    lightness, a_value, b_value = lab
    fy = (lightness + 16) / 116
    fx = a_value / 500 + fy
    fz = fy - b_value / 200

    def inverse(value: float) -> float:
        cube = value**3
        return cube if cube > 0.008856 else (value - 16 / 116) / 7.787

    x = 0.95047 * inverse(fx)
    y = inverse(fy)
    z = 1.08883 * inverse(fz)
    red = 3.2404542 * x - 1.5371385 * y - 0.4985314 * z
    green = -0.9692660 * x + 1.8760108 * y + 0.0415560 * z
    blue = 0.0556434 * x - 0.2040259 * y + 1.0572252 * z
    channels = tuple(_clamp_channel(_srgb_channel(value)) for value in (red, green, blue))
    return cast(tuple[int, int, int], channels)


def _color_lab(profile: DisplayProfile, name: str) -> tuple[float, float, float]:
    color = next(color for color in profile.colors if color.name == name)
    return color.lab or _lab(color.rgb)


def palette_for_profile(
    profile_key: str,
    *,
    rgb_values: Mapping[str, Sequence[int]] | None = None,
    lab_values: Mapping[str, Sequence[float]] | None = None,
    palette_version: str = "custom-1",
) -> DisplayProfile:
    """Return a request-local profile; built-in theoretical RGB values stay immutable."""
    profile = get_display_profile(profile_key)
    if rgb_values and lab_values:
        raise ValueError("RENDER-006 自訂色盤只能選 RGB 或 Lab 其中一種")
    if not rgb_values and not lab_values:
        return profile
    colors = []
    for color in profile.colors:
        if rgb_values and color.name in rgb_values:
            values = tuple(int(value) for value in rgb_values[color.name])
            if len(values) != 3 or any(value < 0 or value > 255 for value in values):
                raise ValueError(f"RENDER-006 {color.name} RGB 必須為 0 到 255 的三個數值")
            rgb = cast(tuple[int, int, int], values)
            colors.append(replace(color, rgb=rgb, lab=_lab(rgb)))
        elif lab_values and color.name in lab_values:
            lab_values_tuple = tuple(float(value) for value in lab_values[color.name])
            if (
                len(lab_values_tuple) != 3
                or not 0 <= lab_values_tuple[0] <= 100
                or any(abs(value) > 160 for value in lab_values_tuple[1:])
            ):
                raise ValueError(f"RENDER-006 {color.name} Lab 數值超出範圍")
            lab = cast(tuple[float, float, float], lab_values_tuple)
            colors.append(replace(color, rgb=_lab_to_rgb(lab), lab=lab))
        else:
            colors.append(replace(color, lab=_color_lab(profile, color.name)))
    return replace(profile, colors=tuple(colors), palette_version=palette_version[:80])


@lru_cache(maxsize=16)
def _palette_lookup(colors: tuple[PaletteColor, ...], distance: str) -> bytes:
    if distance not in COLOR_DISTANCES:
        raise ValueError(f"RENDER-004 不支援的色差模式：{distance}")
    palette_points = [_oklab(color.rgb) for color in colors]
    lookup = bytearray(32 * 32 * 32)
    for red5 in range(32):
        for green5 in range(32):
            for blue5 in range(32):
                rgb = (red5 * 8 + 4, green5 * 8 + 4, blue5 * 8 + 4)
                point = _oklab(rgb) if distance == "oklab" else rgb
                candidates = palette_points if distance == "oklab" else [color.rgb for color in colors]
                nearest = min(
                    range(len(candidates)),
                    key=lambda index: sum(
                        (float(point[channel]) - float(candidates[index][channel])) ** 2
                        for channel in range(3)
                    ),
                )
                lookup[(red5 << 10) | (green5 << 5) | blue5] = nearest
    return bytes(lookup)


def _pillow_palette(colors: tuple[tuple[int, int, int], ...]) -> Image.Image:
    palette = Image.new("P", (1, 1))
    flattened = tuple(channel for color in colors for channel in color)
    palette.putpalette(flattened + (0, 0, 0) * (256 - len(colors)))
    return palette


def _quantize_gooddisplay(
    rgb: Image.Image,
    profile: DisplayProfile,
) -> tuple[bytearray, Image.Image, tuple[PaletteColor, ...]]:
    palette_rgb: tuple[tuple[int, int, int], ...]
    index_mapping: tuple[int, ...]
    if profile.key == "gdep073e01_6c":
        palette_rgb = _GOODDISPLAY_GDEP073E01_PALETTE
        index_mapping = _GOODDISPLAY_GDEP073E01_TO_PROFILE
    else:
        palette_rgb = tuple(color.rgb for color in profile.colors)
        index_mapping = tuple(range(len(profile.colors)))
    quantized = rgb.quantize(
        palette=_pillow_palette(palette_rgb),
        dither=Image.Dither.FLOYDSTEINBERG,
    )
    indexes = bytearray(index_mapping[index] for index in quantized.tobytes())
    return indexes, quantized.convert("RGB"), _gooddisplay_colors(profile)


def _clamp_channel(value: float) -> int:
    return max(0, min(255, int(round(value))))


def _nearest(lookup: bytes, red: float, green: float, blue: float) -> int:
    r, g, b = (_clamp_channel(value) for value in (red, green, blue))
    return lookup[((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3)]


_BAYER_4 = (
    (0, 8, 2, 10),
    (12, 4, 14, 6),
    (3, 11, 1, 9),
    (15, 7, 13, 5),
)
_BAYER_8 = (
    (0, 48, 12, 60, 3, 51, 15, 63),
    (32, 16, 44, 28, 35, 19, 47, 31),
    (8, 56, 4, 52, 11, 59, 7, 55),
    (40, 24, 36, 20, 43, 27, 39, 23),
    (2, 50, 14, 62, 1, 49, 13, 61),
    (34, 18, 46, 30, 33, 17, 45, 29),
    (10, 58, 6, 54, 9, 57, 5, 53),
    (42, 26, 38, 22, 41, 25, 37, 21),
)


def _quantize_ordered(
    rgb: Image.Image,
    profile: DisplayProfile,
    lookup: bytes,
    algorithm: str,
    strength: float,
) -> tuple[bytearray, Image.Image]:
    source = cast(Any, rgb.load())
    preview = Image.new("RGB", rgb.size)
    target = cast(Any, preview.load())
    indexes = bytearray(rgb.width * rgb.height)
    matrix = _BAYER_4 if algorithm == "bayer4" else _BAYER_8
    matrix_size = len(matrix)
    amplitude = 72.0 * strength if algorithm != "none" else 0.0
    for y in range(rgb.height):
        for x in range(rgb.width):
            red, green, blue = cast(tuple[int, int, int], source[x, y])
            threshold = ((matrix[y % matrix_size][x % matrix_size] + 0.5) / (matrix_size**2) - 0.5)
            perturbation = threshold * amplitude
            palette_index = _nearest(
                lookup, red + perturbation, green + perturbation, blue + perturbation
            )
            indexes[y * rgb.width + x] = palette_index
            target[x, y] = profile.colors[palette_index].rgb
    return indexes, preview


def _add_error(buffer: list[float], x: int, error: tuple[float, float, float], weight: float) -> None:
    offset = (x + 2) * 3
    for channel in range(3):
        buffer[offset + channel] += error[channel] * weight


def _quantize_diffusion(
    rgb: Image.Image,
    profile: DisplayProfile,
    lookup: bytes,
    algorithm: str,
    strength: float,
    *,
    linear_light: bool = False,
    protected_mask: Image.Image | None = None,
) -> tuple[bytearray, Image.Image]:
    source = cast(Any, rgb.load())
    preview = Image.new("RGB", rgb.size)
    target = cast(Any, preview.load())
    indexes = bytearray(rgb.width * rgb.height)
    row_size = (rgb.width + 4) * 3
    current = [0.0] * row_size
    following = [0.0] * row_size
    second = [0.0] * row_size
    mask = cast(Any, protected_mask.convert("1").load()) if protected_mask is not None else None
    monochrome = [
        index for index, color in enumerate(profile.colors) if color.name in {"black", "white"}
    ]

    def protected(x: int, y: int) -> bool:
        return bool(mask is not None and 0 <= x < rgb.width and 0 <= y < rgb.height and mask[x, y])

    def distribute(buffer: list[float], x: int, target_y: int, error, weight: float) -> None:
        if 0 <= x < rgb.width and not protected(x, target_y):
            _add_error(buffer, x, error, weight)

    for y in range(rgb.height):
        direction = 1 if y % 2 == 0 else -1
        x_values = range(rgb.width) if direction == 1 else range(rgb.width - 1, -1, -1)
        for x in x_values:
            offset = (x + 2) * 3
            original = cast(tuple[int, int, int], source[x, y])
            base = tuple(_linear_channel(float(value)) for value in original) if linear_light else original
            adjusted = tuple(float(base[channel]) + current[offset + channel] for channel in range(3))
            match_rgb = tuple(_srgb_channel(value) for value in adjusted) if linear_light else adjusted
            if protected(x, y) and len(monochrome) == 2:
                luminance = sum(match_rgb) / 3
                palette_index = min(
                    monochrome,
                    key=lambda index: abs(sum(profile.colors[index].rgb) / 3 - luminance),
                )
            else:
                palette_index = _nearest(lookup, *match_rgb)
            chosen = profile.colors[palette_index].rgb
            indexes[y * rgb.width + x] = palette_index
            target[x, y] = chosen
            chosen_space = tuple(_linear_channel(float(value)) for value in chosen) if linear_light else chosen
            error = tuple((adjusted[channel] - chosen_space[channel]) * strength for channel in range(3))
            if protected(x, y):
                continue
            if algorithm in {"floyd_steinberg", "serpentine_floyd_steinberg"}:
                distribute(current, x + direction, y, error, 7 / 16)
                distribute(following, x - direction, y + 1, error, 3 / 16)
                distribute(following, x, y + 1, error, 5 / 16)
                distribute(following, x + direction, y + 1, error, 1 / 16)
            else:
                distribute(current, x + direction, y, error, 1 / 8)
                distribute(current, x + 2 * direction, y, error, 1 / 8)
                distribute(following, x - direction, y + 1, error, 1 / 8)
                distribute(following, x, y + 1, error, 1 / 8)
                distribute(following, x + direction, y + 1, error, 1 / 8)
                distribute(second, x, y + 2, error, 1 / 8)
        current, following = following, second if algorithm == "atkinson" else [0.0] * row_size
        if algorithm == "atkinson":
            second = [0.0] * row_size
    return indexes, preview


def encode_image(
    image: Image.Image,
    *,
    profile_key: str = "safe_4c",
    dither: str = "floyd_steinberg",
    color_distance: str = "oklab",
    strength: float = 1.0,
    linear_light: bool = False,
    protected_mask: Image.Image | None = None,
    profile: DisplayProfile | None = None,
) -> EncodedImage:
    profile = profile or get_display_profile(profile_key)
    if profile.key != profile_key:
        raise ValueError("RENDER-006 自訂色盤與面板 Profile 不一致")
    if dither not in DITHER_ALGORITHMS:
        raise ValueError(f"RENDER-004 不支援的抖動算法：{dither}")
    if not 0.0 <= float(strength) <= 2.0:
        raise ValueError("RENDER-004 抖動強度必須介於 0 到 2")
    rgb = image.convert("RGB")
    preview_palette = profile.colors
    if dither == "gooddisplay":
        indexes, preview, preview_palette = _quantize_gooddisplay(rgb, profile)
    elif dither == "photo_smooth":
        smoothed = rgb.filter(ImageFilter.MedianFilter(size=3))
        indexes, preview, preview_palette = _quantize_gooddisplay(smoothed, profile)
    else:
        working_profile = profile
        working_dither = {
            "nearest": "none",
            "bayer_ordered": "bayer8",
            "serpentine_floyd_steinberg": "serpentine_floyd_steinberg",
        }.get(dither, dither)
        working_distance = color_distance
        lookup = _palette_lookup(working_profile.colors, working_distance)
        if working_dither in {"floyd_steinberg", "serpentine_floyd_steinberg", "atkinson"} and strength > 0:
            indexes, preview = _quantize_diffusion(
                rgb,
                working_profile,
                lookup,
                working_dither,
                float(strength),
                linear_light=linear_light,
                protected_mask=protected_mask,
            )
        else:
            indexes, preview = _quantize_ordered(
                rgb,
                working_profile,
                lookup,
                working_dither,
                float(strength),
            )

    if profile.pixel_format == "2bpp":
        payload = bytearray((len(indexes) + 3) // 4)
        for index, palette_index in enumerate(indexes):
            payload[index // 4] |= profile.colors[palette_index].code << (6 - (index % 4) * 2)
    else:
        payload = bytearray((len(indexes) + 1) // 2)
        for index, palette_index in enumerate(indexes):
            payload[index // 2] |= profile.colors[palette_index].code << (4 if index % 2 == 0 else 0)
    return EncodedImage(bytes(payload), preview, preview_palette)
