from __future__ import annotations

from PIL import Image, ImageDraw

from inktime.app.domain.rendering import (
    analyze_crop_focus,
    evaluate_e6_suitability,
    fit_with_focus,
)


def test_smart_crop_and_e6_metrics_are_bounded():
    image = Image.new("RGB", (1200, 800), "#d9c3a4")
    draw = ImageDraw.Draw(image)
    draw.ellipse((760, 160, 1060, 520), fill="#a75b45")
    draw.rectangle((30, 600, 1170, 760), fill="#163d72")

    crop = analyze_crop_focus(image)
    suitability = evaluate_e6_suitability(image)

    assert 0 <= crop.focus_x <= 1
    assert 0 <= crop.focus_y <= 1
    assert crop.method in {"faces", "saliency"}
    assert 0 <= suitability.score <= 100
    assert 0 <= suitability.contrast_score <= 100
    assert 0 <= suitability.subject_score <= 100
    assert 0 <= suitability.skin_score <= 100
    assert 0 <= suitability.text_score <= 100


def test_fit_with_focus_keeps_requested_side_and_output_size():
    image = Image.new("RGB", (1000, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((800, 0, 999, 499), fill="red")

    left = fit_with_focus(image, (300, 500), focus_x=0.0)
    right = fit_with_focus(image, (300, 500), focus_x=1.0)

    assert left.size == (300, 500)
    assert right.size == (300, 500)
    assert right.getpixel((299, 250))[1] < left.getpixel((299, 250))[1]
