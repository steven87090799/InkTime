from __future__ import annotations

from itertools import product

from PIL import Image, ImageDraw
import pytest

from inktime.app.domain.rendering.layout_geometry import (
    resolve_compact_pair_geometry,
)
from inktime.app.domain.rendering.text_mask import render_plan_text_mask
from inktime.app.services.rendering import RenderService


@pytest.mark.parametrize("sizes", list(product([(3265, 4898), (900, 1600), (900, 1000), (100, 1600)], repeat=2)))
def test_pairs_use_full_photo_height_with_dates_inside_left_columns(sizes):
    geometry = resolve_compact_pair_geometry(sizes)
    assert geometry.info == ()  # no separate bottom date band
    assert sum(photo.width for photo in geometry.photo_rects) == 684
    for photo, caption in zip(
        geometry.photo_rects, geometry.caption_rects, strict=True
    ):
        assert caption.right < photo.x
        assert caption.width >= 24  # readable CJK column, even with a very narrow source
        assert photo.y == caption.y == 0
        assert photo.height == caption.height == 480
        for rect in (photo, caption):
            assert min(rect.x, rect.y) >= 0
            assert rect.right <= 800 and rect.bottom <= 480
    assert geometry.secondary_caption.x - geometry.primary_photo.right == 8


def test_center_fill_preserves_proportions_without_focus_or_extra_zoom(monkeypatch):
    service = object.__new__(RenderService)
    source = Image.new("RGB", (900, 1600), "white")
    ImageDraw.Draw(source).ellipse((350, 700, 550, 900), fill="red")
    def reject_focus(*args, **kwargs):
        raise AssertionError("compact photos must not use subject-aware fitting")
    monkeypatch.setattr(service, "_fit_photo", reject_focus)
    with service._fit_compact_pair_photo(
        source, (342, 480), fit_mode="cover",
    ) as fitted:
        bounds = fitted.getchannel("G").point(lambda value: 255 if value < 32 else 0).getbbox()
        assert bounds is not None
        width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        assert abs(width - height) <= 2
        assert abs(width - 200 * (342 / 900)) <= 2  # only the scale needed to fill
        assert abs((bounds[0] + bounds[2]) / 2 - 171) <= 1
        assert abs((bounds[1] + bounds[3]) / 2 - 240) <= 1


def test_contain_keeps_edges_when_explicitly_selected():
    source = Image.new("RGB", (900, 1600), "white")
    ImageDraw.Draw(source).rectangle((0, 0, 899, 1599), outline="red", width=20)
    with RenderService._fit_compact_pair_photo(source, (342, 480), fit_mode="contain") as fitted:
        assert fitted.getpixel((0, 240)) == (255, 255, 255)
        for point in ((37, 240), (304, 240), (171, 0), (171, 479)):
            assert fitted.getpixel(point)[0] > 240 and fitted.getpixel(point)[1] < 20


def test_text_and_paper_mask_leaves_photos_unprotected_in_physical_orientation():
    geometry = resolve_compact_pair_geometry(((3265, 4898), (3265, 4898)))
    plan = {"geometry": geometry.as_dict(), "auto_pair_source_sizes": [[3265, 4898]] * 2, "orientation": "landscape"}
    with render_plan_text_mask(plan) as mask:
        assert mask.mode == "1" and mask.size == (480, 800)
        with mask.transpose(Image.Transpose.ROTATE_90) as logical:
            for rect in (*geometry.caption_rects, *geometry.info):
                assert logical.getpixel((rect.x + 1, rect.y + 1))
            for rect in geometry.photo_rects:
                assert not logical.getpixel((rect.x, rect.y))
                assert not logical.getpixel((rect.right - 1, rect.bottom - 1))
            assert logical.getpixel((0, 0))
            assert logical.getpixel((400, 200))
    with pytest.raises(ValueError):
        mask.getpixel((0, 0))  # context exits release the bit-packed buffer
    with pytest.raises(RuntimeError, match="encode failed"):
        with render_plan_text_mask(plan) as failed_mask:
            raise RuntimeError("encode failed")
    with pytest.raises(ValueError):
        failed_mask.getpixel((0, 0))
    with render_plan_text_mask({"layout": "photo_pair"}) as legacy:
        assert legacy is None
    with Image.new("1", (480, 800)) as explicit:
        with render_plan_text_mask(plan, override=explicit) as borrowed:
            assert borrowed is explicit
        assert explicit.getpixel((0, 0)) == 0


@pytest.mark.parametrize("sizes", [((0, 100), (100, 200)), ((100, -1), (100, 200))])
def test_compact_geometry_rejects_invalid_source_dimensions(sizes):
    with pytest.raises(ValueError):
        resolve_compact_pair_geometry(sizes)
