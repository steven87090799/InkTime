from __future__ import annotations

from itertools import product

from PIL import Image, ImageDraw
import pytest

from inktime.app.domain.rendering.layout_geometry import (
    AUTO_PAIR_MAX_ZOOM,
    compact_pair_fit_size,
    resolve_compact_pair_geometry,
)
from inktime.app.domain.rendering.text_mask import render_plan_text_mask
from inktime.app.services.rendering import RenderService


@pytest.mark.parametrize("fit_mode", ["cover", "contain"])
@pytest.mark.parametrize("sizes", list(product([(3265, 4898), (900, 1600), (900, 1000), (100, 1600)], repeat=2)))
def test_pairs_keep_readable_separate_captions_and_dates_inside_canvas(sizes, fit_mode):
    geometry = resolve_compact_pair_geometry(sizes, fit_mode=fit_mode)
    for photo, caption, date, source_size in zip(
        geometry.photo_rects, geometry.caption_rects, geometry.info, sizes, strict=True
    ):
        assert caption.right < photo.x
        assert caption.width >= 24  # readable CJK column, even with a very narrow source
        assert date.width >= 160
        assert date.y >= photo.bottom
        for rect in (photo, caption, date):
            assert min(rect.x, rect.y) >= 0
            assert rect.right <= 800 and rect.bottom <= 480
        fitted = compact_pair_fit_size(source_size, (photo.width, photo.height), fit_mode=fit_mode)
        # The visible crop cannot expand into arbitrary cover magnification.
        scale = max(fitted[0] / source_size[0], fitted[1] / source_size[1])
        contain = min(photo.width / source_size[0], photo.height / source_size[1])
        assert scale <= contain * (1 if fit_mode == "contain" else AUTO_PAIR_MAX_ZOOM) + 0.001
    assert geometry.secondary_caption.x - geometry.primary_photo.right == 12


def test_subject_at_bottom_survives_bounded_enlargement_without_distortion():
    service = object.__new__(RenderService)
    source = Image.new("RGB", (900, 1600), "white")
    ImageDraw.Draw(source).rectangle((200, 1510, 700, 1599), fill="red")
    photo = {
        "crop_manual_x": None, "crop_manual_y": None, "crop_focus_x": .5, "crop_focus_y": .5,
        "crop_subject_left": .2, "crop_subject_top": .4, "crop_subject_right": .8, "crop_subject_bottom": 1.,
    }
    with service._fit_compact_pair_photo(
        source, photo, (277, 456), None, None, fit_mode="cover", align_right=True,
    ) as fitted:
        assert fitted.getpixel((138, 455))[0] > 240
        assert fitted.getpixel((138, 455))[1] < 20


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
