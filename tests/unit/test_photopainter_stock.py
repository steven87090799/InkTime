from __future__ import annotations

import struct

from PIL import Image
import pytest

from inktime.app.domain.photopainter.stock_protocol import (
    STOCK_BMP_BYTES,
    STOCK_PAYLOAD_BYTES,
    packed_to_rgb888,
    packed_frame_to_stock_payload,
    portrait_to_physical,
    rgb_image_to_stock_payload,
    stock_dataup_payload,
)


def test_stock_payload_is_exact_mode_byte_plus_bottom_up_24bit_bmp():
    payload = rgb_image_to_stock_payload(Image.new("RGB", (800, 480), (255, 0, 0)))

    assert len(payload) == STOCK_PAYLOAD_BYTES == 1_152_055
    assert payload[0] == 1
    assert payload[1:3] == b"BM"
    assert struct.unpack_from("<I", payload, 1 + 2)[0] == 1_152_054
    assert struct.unpack_from("<iiHH", payload, 1 + 18) == (800, 480, 1, 24)
    assert payload[1 + 54 : 1 + 57] == bytes((0, 0, 255))


def test_safe_four_color_production_frame_is_converted_at_delivery_boundary():
    production_frame = bytes((0b10101010,)) * 96_000
    payload = packed_frame_to_stock_payload(production_frame, profile_key="safe_4c")

    assert len(payload) == STOCK_PAYLOAD_BYTES
    assert payload[0] == 1
    # Stock delivery uses the protocol's logical RGB palette; the production
    # renderer palette remains unchanged on the server side.
    assert payload[1 + 54 : 1 + 57] == bytes((0, 0, 255))


def test_stock_orientation_matches_the_photo_painter_physical_mapping():
    assert portrait_to_physical(0, 0, rotate180=False) == (799, 0)
    assert portrait_to_physical(479, 0, rotate180=False) == (799, 479)
    assert portrait_to_physical(0, 799, rotate180=False) == (0, 0)
    assert portrait_to_physical(479, 799, rotate180=False) == (0, 479)
    assert portrait_to_physical(0, 0, rotate180=True) == (0, 479)
    assert portrait_to_physical(479, 799, rotate180=True) == (799, 0)


def test_stock_encoder_rejects_invalid_mode_and_palette_payloads():
    with pytest.raises(ValueError):
        rgb_image_to_stock_payload(Image.new("RGB", (800, 480), "white"), mode=2)
    with pytest.raises(ValueError):
        stock_dataup_payload(b"\x00" * 192_000, pixel_format="indexed4", rotate180=False, network_mode=2)
    with pytest.raises(ValueError):
        packed_to_rgb888(b"\x66" * 192_000, pixel_format="indexed4", rotate180=False)
    with pytest.raises(ValueError):
        packed_to_rgb888(b"\x00" * 95_999, pixel_format="2bpp", rotate180=False)


def test_stock_dataup_payload_exposes_the_raw_bmp_size():
    payload = stock_dataup_payload(
        bytes((0x11,)) * 192_000,
        pixel_format="indexed4",
        rotate180=False,
    )
    assert len(payload) == 1 + STOCK_BMP_BYTES == STOCK_PAYLOAD_BYTES
