"""Temporary, bit-packed text/paper protection shared by previews and Releases."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from PIL import Image, ImageDraw


@contextmanager
def render_plan_text_mask(
    plan: Mapping[str, Any] | None, *, override: Image.Image | None = None
) -> Iterator[Image.Image | None]:
    if override is not None:
        yield override
        return
    if not plan or plan.get("auto_pair_source_sizes") is None:
        yield None
        return
    geometry = plan["geometry"]
    width, height = int(geometry["canvas_width"]), int(geometry["canvas_height"])
    if (width, height) != (800, 480) or plan["orientation"] != "landscape":
        raise ValueError("RENDER-005 自動雙照片文字遮罩尺寸無效")
    photo_rects = geometry["photo_rects"]
    if len(photo_rects) != 2:
        raise ValueError("RENDER-005 自動雙照片文字遮罩缺少照片區")
    physical = Image.new("1", (height, width), 1)
    try:
        draw = ImageDraw.Draw(physical)
        for rect in photo_rects:
            x, y, w, h = (int(rect[key]) for key in ("x", "y", "width", "height"))
            if min(x, y) < 0 or min(w, h) <= 0 or x + w > width or y + h > height:
                raise ValueError("RENDER-005 自動雙照片文字區超出畫布")
            # Keep text, dates, gutters and outer paper strictly black/white.
            # Draw directly in physical coordinates: avoid a second rotated
            # mask allocation. Logical (x, y) rotates clockwise to (H-y-1, x).
            draw.rectangle((height - y - h, x, height - y - 1, x + w - 1), fill=0)
        del draw  # ImageDraw holds its own reference to the pixel buffer.
        yield physical
    finally:
        # Pillow's Image context only closes file handles. These masks are
        # created in memory, so release their pixel buffers explicitly.
        physical.close()
