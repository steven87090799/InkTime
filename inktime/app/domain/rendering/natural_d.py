"""Frozen natural-D recipe derived from epdoptimize 1.3.0 (Apache-2.0).

The accepted D camera preview used mild linear-sRGB white balance, -10% HSL
saturation, display-range LAB compression, white preservation, and serpentine
RGB Floyd–Steinberg. See docs/reference/CURRENT_STATE_ZH_TW.md for the recipe
and third_party/epdoptimize/NOTICE.txt for upstream attribution and changes.

Only fixed-size chunks use floating point arrays. No image or diffusion table
is retained in a module cache; all working storage belongs to this one call.
"""
from __future__ import annotations

from math import floor
from typing import Any

MAX_PIXELS = 480 * 800
CHUNK_PIXELS = 2048
VERSION = "natural-d-epdoptimize-1.3.0-v1"
WHITE_BALANCE_GAINS = (1.0, 0.985, 1.16)
SATURATION_MULTIPLIER = 0.9
SPECTRA6 = (
    ("black", (2, 2, 2)),
    ("white", (190, 200, 200)),
    ("blue", (5, 64, 158)),
    ("green", (39, 102, 60)),
    ("red", (135, 19, 0)),
    ("yellow", (205, 202, 0)),
)


def _byte(values: Any) -> Any:
    import numpy as np

    # epdoptimize clampByte uses Math.round, including half-up diffusion ties.
    return np.floor(np.clip(values, 0, 255) + 0.5).astype(np.uint8)


def _luma(values: Any) -> Any:
    return values[..., 0] * 0.2126 + values[..., 1] * 0.7152 + values[..., 2] * 0.0722


def _saturation(values: Any) -> Any:
    import numpy as np

    high = values.max(axis=-1)
    return (high - values.min(axis=-1)) / np.maximum(high, 1)


def _white_balance(values: Any) -> Any:
    import numpy as np

    # Freeze the same float32 operation order used for the reviewed D preview.
    encoded = values.astype(np.float32) / 255
    linear = np.where(encoded <= 0.04045, encoded / 12.92, ((encoded + 0.055) / 1.055) ** 2.4)
    gains = np.array(WHITE_BALANCE_GAINS, dtype=np.float32)
    normalization = float(np.dot(gains, np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)))
    adjusted = np.clip(linear * (gains / normalization), 0, 1)
    encoded = np.where(adjusted <= 0.0031308, adjusted * 12.92,
                       1.055 * np.power(adjusted, 1 / 2.4) - 0.055)
    return np.rint(np.clip(encoded, 0, 1) * 255).astype(np.uint8)


def _tone(values: Any) -> Any:
    import numpy as np

    rgb = values.astype(np.float64) / 255
    red, green, blue = rgb.T
    high, low = rgb.max(axis=1), rgb.min(axis=1)
    lightness = (high + low) / 2
    delta = high - low
    safe_delta = np.where(delta == 0, 1, delta)
    denominator = np.where(lightness > 0.5, 2 - high - low, high + low)
    saturation = delta / np.maximum(denominator, 0.000001)
    hue = np.where(high == red, ((green - blue) / safe_delta + np.where(green < blue, 6, 0)) / 6,
                   np.where(high == green, ((blue - red) / safe_delta + 2) / 6,
                            ((red - green) / safe_delta + 4) / 6))
    chroma = (1 - np.abs(2 * lightness - 1)) * np.clip(saturation * SATURATION_MULTIPLIER, 0, 1)
    cross = chroma * (1 - np.abs((hue * 6) % 2 - 1))
    offset = lightness - chroma / 2
    sector = np.floor(hue * 6)
    zero = np.zeros_like(chroma)
    channels = (
        np.select([sector == 0, sector == 1, sector == 2, sector == 3, sector == 4],
                  [chroma, cross, zero, zero, cross], default=chroma),
        np.select([sector == 0, sector == 1, sector == 2, sector == 3, sector == 4],
                  [cross, chroma, chroma, cross, zero], default=zero),
        np.select([sector == 0, sector == 1, sector == 2, sector == 3, sector == 4],
                  [zero, zero, cross, chroma, chroma], default=cross),
    )
    toned = np.stack(channels, axis=1) + offset[:, None]
    toned[delta == 0] = rgb[delta == 0]
    return _byte(toned * 255)


def _lab(values: Any) -> Any:
    import numpy as np

    encoded = values.astype(np.float64) / 255
    linear = np.where(encoded <= 0.04045, encoded / 12.92, ((encoded + 0.055) / 1.055) ** 2.4)
    r, g, b = linear.T
    xyz = np.stack(((r * 0.4124564 + g * 0.3575761 + b * 0.1804375) * 100 / 95.047,
                    r * 0.2126729 + g * 0.7151522 + b * 0.072175,
                    (r * 0.0193339 + g * 0.119192 + b * 0.9503041) * 100 / 108.883), axis=1)
    pivot = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    x, y, z = pivot.T
    return np.stack((116 * y - 16, 500 * (x - y), 200 * (y - z)), axis=1)


def _from_lab(lightness: Any, a: Any, b: Any) -> Any:
    import numpy as np

    y = (lightness + 16) / 116
    xyz = np.stack((a / 500 + y, y, y - b / 200), axis=1)
    xyz = np.where(xyz > 0.206897, xyz ** 3, (xyz - 16 / 116) / 7.787)
    x, y, z = (xyz * np.array([95.047, 100, 108.883]) / 100).T
    rgb = np.stack((x * 3.2404542 + y * -1.5371385 + z * -0.4985314,
                    x * -0.969266 + y * 1.8760108 + z * 0.041556,
                    x * 0.0556434 + y * -0.2040259 + z * 1.0572252), axis=1)
    encoded = np.where(rgb > 0.0031308, 1.055 * np.power(np.maximum(rgb, 0), 1 / 2.4) - 0.055,
                       12.92 * rgb)
    return _byte(encoded * 255)


def _compress(values: Any, endpoints: Any) -> Any:
    import numpy as np

    lab = _lab(values)
    lightness, a, b = lab.T
    saturation = _saturation(values.astype(np.float64))
    progress = np.clip((saturation - 0.18) / (0.68 - 0.18), 0, 1)
    strength = 1 - progress * progress * (3 - 2 * progress) * 0.85
    target = endpoints[0, 0] + np.clip(lightness / 100, 0, 1) * (endpoints[1, 0] - endpoints[0, 0])
    result = _from_lab(lightness + (target - lightness) * strength, a, b)
    source_luma = _luma(values.astype(np.float64))

    def protected(candidate: Any) -> Any:
        return ((saturation < 0.16)
                | (_saturation(candidate.astype(np.float64)) >= np.maximum(0.12, saturation * 0.72))
                | (_luma(candidate.astype(np.float64)) <= source_luma + 4))

    search = (target > lightness) & ~protected(result)
    if np.any(search):
        low, high = np.zeros_like(strength), strength.copy()
        fitted = values.copy()
        for _ in range(5):
            middle = (low + high) / 2
            candidate = _from_lab(lightness + (target - lightness) * middle, a, b)
            accepted = protected(candidate)
            fitted[accepted] = candidate[accepted]
            low = np.where(accepted, middle, low)
            high = np.where(accepted, high, middle)
        result[search] = fitted[search]
    return result


def _prepare(source: bytes, palette: tuple[tuple[int, int, int], ...]) -> bytearray:
    import numpy as np

    work = bytearray(len(source))
    histogram = np.zeros(256, dtype=np.int64)
    for start in range(0, len(source), CHUNK_PIXELS * 3):
        block = np.frombuffer(source[start:start + CHUNK_PIXELS * 3], dtype=np.uint8).reshape(-1, 3)
        balanced = _white_balance(block)
        work[start:start + balanced.size] = balanced.tobytes()
        candidate = _saturation(balanced.astype(np.float64)) <= 0.22
        levels = _byte(_luma(balanced.astype(np.float64)))
        histogram += np.bincount(levels[candidate], minlength=256)
    count = int(histogram.sum())
    threshold = None
    if count:
        rank = min(count - 1, floor((count - 1) * 0.97 + 0.5))
        level = int(np.searchsorted(np.cumsum(histogram), rank, side="right"))
        if level >= 200:
            threshold = level
    colors = np.array(palette, dtype=np.uint8)
    luminance = _luma(colors.astype(np.float64))
    black, white = colors[int(luminance.argmin())], colors[int(luminance.argmax())]
    endpoints = _lab(np.stack((black, white)))
    white_luma = float(_luma(white.astype(np.float64)))
    for start in range(0, len(work), CHUNK_PIXELS * 3):
        block = np.frombuffer(work[start:start + CHUNK_PIXELS * 3], dtype=np.uint8).reshape(-1, 3)
        adjusted = _compress(_tone(block), endpoints)
        if threshold is not None:
            bright = ((_saturation(block.astype(np.float64)) <= 0.22)
                      & (_luma(block.astype(np.float64)) + 0.0001 >= threshold)
                      & (_luma(adjusted.astype(np.float64)) < white_luma))
            adjusted[bright] = white
        work[start:start + adjusted.size] = adjusted.tobytes()
    return work


def quantize(
    source: bytes, width: int, height: int,
    palette: tuple[tuple[int, int, int], ...], protected: bytes | None = None,
) -> bytearray:
    """Return palette indexes; reject originals larger than one panel frame."""
    pixels = width * height
    if width <= 0 or height <= 0 or pixels > MAX_PIXELS or len(source) != pixels * 3:
        raise ValueError("RENDER-004 D 色彩模式僅接受不超過 480×800 像素的已排版圖片")
    if not 2 <= len(palette) <= 7 or any(len(c) != 3 or any(not 0 <= v <= 255 for v in c) for c in palette):
        raise ValueError("RENDER-004 D 色彩模式色盤不合法")
    if protected is not None and len(protected) != pixels:
        raise ValueError("RENDER-004 文字保護遮罩尺寸不符")
    import numpy as np

    work = _prepare(source, palette)
    indexes = bytearray(pixels)
    coefficients = tuple((sum(v * v for v in c), 2 * c[0], 2 * c[1], 2 * c[2]) for c in palette)
    values = np.arange(256, dtype=np.int32)[None, :] * 16 + 8
    errors = np.arange(-255, 256, dtype=np.int32)[:, None]
    tables = {n: np.clip((values + errors * n) // 16, 0, 255).astype(np.uint8).tobytes()
              for n in (7, 3, 5, 1)}
    black = min(range(len(palette)), key=lambda i: sum(palette[i]))
    white = max(range(len(palette)), key=lambda i: sum(palette[i]))
    for y in range(height):
        step = 1 if y % 2 == 0 else -1
        for x in (range(width) if step == 1 else range(width - 1, -1, -1)):
            pixel = y * width + x
            offset = pixel * 3
            r, g, b = work[offset:offset + 3]
            if protected is not None and protected[pixel]:
                indexes[pixel] = white if sum(source[offset:offset + 3]) >= 384 else black
                continue
            chosen, distance = 0, 10**9
            for i, (norm, cr, cg, cb) in enumerate(coefficients):
                cost = norm - r * cr - g * cg - b * cb
                if cost < distance:
                    chosen, distance = i, cost
            indexes[pixel] = chosen
            dr = (r - palette[chosen][0] + 255) * 256
            dg = (g - palette[chosen][1] + 255) * 256
            db = (b - palette[chosen][2] + 255) * 256
            for dx, dy, weight in ((step, 0, 7), (-step, 1, 3), (0, 1, 5), (step, 1, 1)):
                nx, ny = x + dx, y + dy
                if not (0 <= nx < width and ny < height):
                    continue
                neighbor = ny * width + nx
                if protected is not None and protected[neighbor]:
                    continue
                neighbor *= 3
                table = tables[weight]
                work[neighbor] = table[dr + work[neighbor]]
                work[neighbor + 1] = table[dg + work[neighbor + 1]]
                work[neighbor + 2] = table[db + work[neighbor + 2]]
    return indexes
