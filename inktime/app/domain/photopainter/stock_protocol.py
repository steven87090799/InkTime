"""Waveshare ESP32-S3-PhotoPainter Stock `/dataUP` protocol.

The compatibility path deliberately consumes the existing production binary
format and converts it at the delivery boundary.  The normal renderer keeps
emitting the panel-native 96 KiB/192 KiB payloads.
"""

from __future__ import annotations

import struct
from typing import Callable

from PIL import Image

from inktime.app.domain.rendering.palette import get_display_profile


STOCK_MODE_BASIC = 1
STOCK_WIDTH = 800
STOCK_HEIGHT = 480
STOCK_BMP_HEADER_BYTES = 54
STOCK_PIXEL_BYTES = STOCK_WIDTH * STOCK_HEIGHT * 3
STOCK_BMP_BYTES = STOCK_BMP_HEADER_BYTES + STOCK_PIXEL_BYTES
STOCK_PAYLOAD_BYTES = 1 + STOCK_BMP_BYTES
STOCK_STA_PAYLOAD_BYTES = STOCK_PAYLOAD_BYTES
STOCK_SUPPORTED_PROFILES = {"safe_4c", "gdep073e01_6c"}
INDEXED4_RGB = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "red": (255, 0, 0),
    "yellow": (255, 255, 0),
}
SAFE4_RGB = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "yellow": (255, 255, 0),
}


def read_indexed4(payload: bytes, pixel: int) -> int:
    """Read one portrait indexed4 pixel (high nibble first)."""
    if pixel < 0 or pixel // 2 >= len(payload):
        raise ValueError("PHOTOPAINTER-001 indexed4 像素位置超出範圍")
    packed = payload[pixel // 2]
    return packed >> 4 if pixel % 2 == 0 else packed & 0x0F


def read_2bpp(payload: bytes, pixel: int) -> int:
    """Read one portrait 2bpp pixel (MSB first)."""
    if pixel < 0 or pixel // 4 >= len(payload):
        raise ValueError("PHOTOPAINTER-001 2bpp 像素位置超出範圍")
    packed = payload[pixel // 4]
    return (packed >> (6 - (pixel % 4) * 2)) & 0x03


def portrait_to_physical(x: int, y: int, *, rotate180: bool) -> tuple[int, int]:
    """Map the 480x800 logical frame to the 800x480 Stock physical frame."""
    if not 0 <= x < 480 or not 0 <= y < 800:
        raise ValueError("PHOTOPAINTER-001 logical coordinate out of range")
    if rotate180:
        px, py = y, 479 - x
    else:
        px, py = 799 - y, x
    if not 0 <= px < STOCK_WIDTH or not 0 <= py < STOCK_HEIGHT:
        raise AssertionError((px, py))
    return px, py


def packed_to_rgb888(
    payload: bytes,
    *,
    pixel_format: str,
    rotate180: bool,
) -> bytes:
    """Convert one portrait Production BIN into row-major physical RGB888."""
    if pixel_format == "indexed4":
        expected = 192_000
        reader: Callable[[bytes, int], int] = read_indexed4
        palette = {
            0: INDEXED4_RGB["black"],
            1: INDEXED4_RGB["white"],
            2: INDEXED4_RGB["green"],
            3: INDEXED4_RGB["blue"],
            4: INDEXED4_RGB["red"],
            5: INDEXED4_RGB["yellow"],
        }
    elif pixel_format == "2bpp":
        expected = 96_000
        reader = read_2bpp
        palette = {
            0: SAFE4_RGB["black"],
            1: SAFE4_RGB["white"],
            2: SAFE4_RGB["red"],
            3: SAFE4_RGB["yellow"],
        }
    else:
        raise ValueError("PHOTOPAINTER-001 不支援的 Production BIN 格式")
    if len(payload) != expected:
        raise ValueError("PHOTOPAINTER-001 Production BIN 大小不符合面板 Profile")
    output = bytearray(STOCK_PIXEL_BYTES)
    for y in range(800):
        for x in range(480):
            code = reader(payload, y * 480 + x)
            try:
                red, green, blue = palette[code]
            except KeyError as exc:
                raise ValueError("PHOTOPAINTER-002 Production BIN 含未知色碼") from exc
            px, py = portrait_to_physical(x, y, rotate180=rotate180)
            offset = (py * STOCK_WIDTH + px) * 3
            output[offset : offset + 3] = bytes((red, green, blue))
    return bytes(output)


def _unpack_indices(payload: bytes, *, width: int, height: int, pixel_format: str) -> list[int]:
    pixels = width * height
    expected = pixels // (4 if pixel_format == "2bpp" else 2)
    if len(payload) != expected:
        raise ValueError("PHOTOPAINTER-001 Production BIN 大小不符合面板 Profile")
    indices: list[int] = []
    if pixel_format == "2bpp":
        for index in range(pixels):
            indices.append((payload[index // 4] >> (6 - (index % 4) * 2)) & 0x03)
    elif pixel_format == "indexed4":
        for index in range(pixels):
            indices.append((payload[index // 2] >> (4 if index % 2 == 0 else 0)) & 0x0F)
    else:
        raise ValueError("PHOTOPAINTER-001 不支援的 Production BIN 格式")
    return indices


def _rgb_from_packed(payload: bytes, *, profile_key: str, width: int = 480, height: int = 800) -> Image.Image:
    if profile_key not in STOCK_SUPPORTED_PROFILES:
        raise ValueError("PHOTOPAINTER-001 Stock 只支援四色或六色邏輯色盤")
    profile = get_display_profile(profile_key)
    indices = _unpack_indices(payload, width=width, height=height, pixel_format=profile.pixel_format)
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    if pixels is None:
        raise ValueError("PHOTOPAINTER-004 無法配置 BMP 像素緩衝")
    try:
        colors = {color.code: INDEXED4_RGB[color.name] for color in profile.colors}
    except KeyError as exc:
        raise ValueError("PHOTOPAINTER-002 Stock Profile 含不支援的邏輯色碼") from exc
    for index, code in enumerate(indices):
        if code not in colors:
            raise ValueError("PHOTOPAINTER-002 Production BIN 含未知色碼")
        pixels[index % width, index // width] = colors[code]
    return image


def encode_bmp24(rgb: bytes, width: int = STOCK_WIDTH, height: int = STOCK_HEIGHT) -> bytes:
    """Encode row-major RGB888 as a bottom-up, uncompressed BMP24."""
    if width <= 0 or height <= 0 or len(rgb) != width * height * 3:
        raise ValueError("PHOTOPAINTER-003 RGB length mismatch")
    raw_row = width * 3
    stride = (raw_row + 3) & ~3
    pixel_bytes = stride * height
    body = bytearray(pixel_bytes)
    output = 0
    for y in range(height - 1, -1, -1):
        row = y * raw_row
        for x in range(width):
            source = row + x * 3
            red, green, blue = rgb[source : source + 3]
            body[output : output + 3] = bytes((blue, green, red))
            output += 3
        output += stride - raw_row
    header = struct.pack(
        "<2sIHHI IiiHHIIiiII",
        b"BM",
        STOCK_BMP_HEADER_BYTES + pixel_bytes,
        0,
        0,
        STOCK_BMP_HEADER_BYTES,
        40,
        width,
        height,
        1,
        24,
        0,
        pixel_bytes,
        2835,
        2835,
        0,
        0,
    )
    assert len(header) == STOCK_BMP_HEADER_BYTES
    result = header + bytes(body)
    if (width, height) == (STOCK_WIDTH, STOCK_HEIGHT) and len(result) != STOCK_BMP_BYTES:
        raise AssertionError(len(result))
    return result


def _bmp24(image: Image.Image) -> bytes:
    image = image.convert("RGB")
    if image.size != (STOCK_WIDTH, STOCK_HEIGHT):
        raise ValueError("PHOTOPAINTER-003 Stock BMP 必須是 800×480")
    return encode_bmp24(image.tobytes(), STOCK_WIDTH, STOCK_HEIGHT)


def rgb_image_to_stock_payload(image: Image.Image, *, mode: int = STOCK_MODE_BASIC) -> bytes:
    """Encode one RGB frame as the exact stock mode-byte-plus-BMP payload."""
    if type(mode) is not int or mode not in {0, 1}:
        raise ValueError("PHOTOPAINTER-004 Stock mode byte 不合法")
    bmp = _bmp24(image)
    payload = bytes([mode]) + bmp
    if len(payload) != STOCK_PAYLOAD_BYTES:
        raise AssertionError("PHOTOPAINTER-005 Stock payload 長度計算錯誤")
    return payload


def stock_dataup_payload(
    payload: bytes,
    *,
    pixel_format: str,
    rotate180: bool,
    network_mode: int = STOCK_MODE_BASIC,
) -> bytes:
    """Build the exact Stock `/dataUP` mode-byte-plus-BMP request body."""
    if type(network_mode) is not int or network_mode not in {0, 1}:
        raise ValueError("PHOTOPAINTER-004 Stock mode byte 只允許 AP=0 或 STA=1")
    rgb = packed_to_rgb888(payload, pixel_format=pixel_format, rotate180=rotate180)
    return bytes((network_mode,)) + encode_bmp24(rgb)


def packed_frame_to_stock_payload(
    payload: bytes,
    *,
    profile_key: str,
    mode: int = STOCK_MODE_BASIC,
    source_width: int = 480,
    source_height: int = 800,
    rotate180: bool = False,
) -> bytes:
    """Convert an existing portrait panel BIN to Stock 800×480 BMP.

    The current production frame maps to the stock panel by a clockwise
    quarter-turn, matching the PhotoPainter portrait-to-physical transform.
    A native 800×480 source is accepted for device-test fixtures as well.
    """
    if (source_width, source_height) == (480, 800):
        profile = get_display_profile(profile_key)
        return stock_dataup_payload(
            payload,
            pixel_format=profile.pixel_format,
            rotate180=rotate180,
            network_mode=mode,
        )
    image = _rgb_from_packed(
        payload, profile_key=profile_key, width=source_width, height=source_height
    )
    return rgb_image_to_stock_payload(image, mode=mode)
