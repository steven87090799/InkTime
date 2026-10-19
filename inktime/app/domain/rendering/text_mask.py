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
    with Image.new("1", (width, height), 1) as logical:
        draw = ImageDraw.Draw(logical)
        for rect in photo_rects:
            x, y, w, h = (int(rect[key]) for key in ("x", "y", "width", "height"))
            if min(x, y) < 0 or min(w, h) <= 0 or x + w > width or y + h > height:
                raise ValueError("RENDER-005 自動雙照片文字區超出畫布")
            # Keep text, dates, gutters and outer paper strictly black/white.
            draw.rectangle((x, y, x + w - 1, y + h - 1), fill=0)
        with logical.transpose(Image.Transpose.ROTATE_270) as physical:
            yield physical
