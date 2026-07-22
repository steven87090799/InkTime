from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, cast

from PIL import Image, ImageFilter, ImageOps, ImageStat

from .palette import encode_image


@dataclass(frozen=True)
class CropAnalysis:
    focus_x: float
    focus_y: float
    subject_left: float
    subject_top: float
    subject_right: float
    subject_bottom: float
    method: str
    face_count: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class E6Suitability:
    score: float
    contrast_score: float
    subject_score: float
    skin_score: float
    text_score: float
    skin_pixels: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def _opencv_faces(image: Image.Image) -> list[tuple[int, int, int, int]]:
    try:
        import cv2
        import numpy as np

        cascade = cv2.CascadeClassifier(
            cast(Any, cv2).data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        if cascade.empty():
            return []
        grayscale = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        detected = cascade.detectMultiScale(
            grayscale,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(max(24, image.width // 16), max(24, image.height // 16)),
        )
        return [cast(tuple[int, int, int, int], tuple(int(value) for value in face)) for face in detected]
    except (ImportError, AttributeError, ValueError):
        return []


def _saliency_focus(image: Image.Image) -> CropAnalysis:
    sample = image.convert("RGB")
    sample.thumbnail((320, 320), Image.Resampling.LANCZOS)
    edge = sample.convert("L").filter(ImageFilter.FIND_EDGES)
    hsv = sample.convert("HSV")
    grid = 8
    cell_width = max(1, sample.width // grid)
    cell_height = max(1, sample.height // grid)
    cells: list[tuple[float, float, float, tuple[float, float, float, float]]] = []
    for row in range(grid):
        for column in range(grid):
            left = column * cell_width
            top = row * cell_height
            right = sample.width if column == grid - 1 else (column + 1) * cell_width
            bottom = sample.height if row == grid - 1 else (row + 1) * cell_height
            box = (left, top, right, bottom)
            edge_mean = float(ImageStat.Stat(edge.crop(box)).mean[0])
            saturation = float(ImageStat.Stat(hsv.crop(box)).mean[1])
            center_x = (left + right) / 2 / sample.width
            center_y = (top + bottom) / 2 / sample.height
            center_prior = max(0.0, 1.0 - (((center_x - 0.5) ** 2 + (center_y - 0.45) ** 2) ** 0.5))
            score = edge_mean * 0.55 + saturation * 0.25 + center_prior * 35
            cells.append((score, center_x, center_y, (
                left / sample.width,
                top / sample.height,
                right / sample.width,
                bottom / sample.height,
            )))
    ordered = sorted(cells, reverse=True)
    chosen = ordered[: max(3, len(ordered) // 8)]
    total = sum(max(0.001, item[0]) for item in chosen)
    focus_x = sum(item[1] * max(0.001, item[0]) for item in chosen) / total
    focus_y = sum(item[2] * max(0.001, item[0]) for item in chosen) / total
    return CropAnalysis(
        focus_x=_clamp(focus_x),
        focus_y=_clamp(focus_y),
        subject_left=min(item[3][0] for item in chosen),
        subject_top=min(item[3][1] for item in chosen),
        subject_right=max(item[3][2] for item in chosen),
        subject_bottom=max(item[3][3] for item in chosen),
        method="saliency",
        face_count=0,
    )


def analyze_crop_focus(image: Image.Image) -> CropAnalysis:
    sample = ImageOps.exif_transpose(image).convert("RGB")
    sample.thumbnail((480, 480), Image.Resampling.LANCZOS)
    faces = _opencv_faces(sample)
    if not faces:
        return _saliency_focus(sample)
    left = min(face[0] for face in faces)
    top = min(face[1] for face in faces)
    right = max(face[0] + face[2] for face in faces)
    bottom = max(face[1] + face[3] for face in faces)
    padding_x = max(8, int((right - left) * 0.18))
    padding_y = max(8, int((bottom - top) * 0.25))
    left = max(0, left - padding_x)
    right = min(sample.width, right + padding_x)
    top = max(0, top - padding_y)
    bottom = min(sample.height, bottom + padding_y)
    return CropAnalysis(
        focus_x=_clamp((left + right) / 2 / sample.width),
        focus_y=_clamp((top + bottom) / 2 / sample.height - 0.03),
        subject_left=left / sample.width,
        subject_top=top / sample.height,
        subject_right=right / sample.width,
        subject_bottom=bottom / sample.height,
        method="faces",
        face_count=len(faces),
    )


def fit_with_focus(
    image: Image.Image,
    size: tuple[int, int],
    *,
    focus_x: float = 0.5,
    focus_y: float = 0.5,
    subject_box: tuple[float, float, float, float] | None = None,
) -> Image.Image:
    source = image.convert("RGB")
    target_width, target_height = size
    source_ratio = source.width / max(1, source.height)
    target_ratio = target_width / max(1, target_height)
    if source_ratio > target_ratio:
        crop_height = float(source.height)
        crop_width = crop_height * target_ratio
        left = _clamp(focus_x) * source.width - crop_width / 2
        if subject_box:
            subject_left = subject_box[0] * source.width
            subject_right = subject_box[2] * source.width
            if subject_right - subject_left <= crop_width:
                left = min(left, subject_left)
                left = max(left, subject_right - crop_width)
        left = max(0.0, min(source.width - crop_width, left))
        box = (left, 0.0, left + crop_width, crop_height)
    else:
        crop_width = float(source.width)
        crop_height = crop_width / target_ratio
        top = _clamp(focus_y) * source.height - crop_height / 2
        if subject_box:
            subject_top = subject_box[1] * source.height
            subject_bottom = subject_box[3] * source.height
            if subject_bottom - subject_top <= crop_height:
                top = min(top, subject_top)
                top = max(top, subject_bottom - crop_height)
        top = max(0.0, min(source.height - crop_height, top))
        box = (0.0, top, crop_width, top + crop_height)
    crop_box = cast(tuple[int, int, int, int], tuple(round(value) for value in box))
    return source.crop(crop_box).resize(
        size, Image.Resampling.LANCZOS
    )


def _edge_energy(image: Image.Image) -> float:
    softened = image.convert("L").filter(ImageFilter.GaussianBlur(radius=0.6))
    return float(ImageStat.Stat(softened.filter(ImageFilter.FIND_EDGES)).mean[0])


def _strong_edge_retention(original: Image.Image, preview: Image.Image) -> float:
    source = original.convert("L")
    target = preview.convert("L")
    source_pixels = source.load()
    target_pixels = target.load()
    assert source_pixels is not None
    assert target_pixels is not None
    strong = retained = 0
    for y in range(0, source.height - 2, 2):
        for x in range(0, source.width - 2, 2):
            original_delta = max(
                abs(int(cast(int, source_pixels[x, y])) - int(cast(int, source_pixels[x + 2, y]))),
                abs(int(cast(int, source_pixels[x, y])) - int(cast(int, source_pixels[x, y + 2]))),
            )
            if original_delta < 55:
                continue
            strong += 1
            target_delta = max(
                abs(int(cast(int, target_pixels[x, y])) - int(cast(int, target_pixels[x + 2, y]))),
                abs(int(cast(int, target_pixels[x, y])) - int(cast(int, target_pixels[x, y + 2]))),
            )
            if target_delta >= 38:
                retained += 1
    return 100.0 if strong < 12 else retained / strong * 100.0


def _skin_fidelity(original: Image.Image, preview: Image.Image) -> tuple[float, int]:
    source = original.convert("RGB")
    target = preview.filter(ImageFilter.GaussianBlur(radius=2)).convert("RGB")
    ycbcr = source.convert("YCbCr")
    source_pixels = source.load()
    target_pixels = target.load()
    skin_pixels = ycbcr.load()
    assert source_pixels is not None
    assert target_pixels is not None
    assert skin_pixels is not None
    count = 0
    error = 0.0
    for y in range(0, source.height, 2):
        for x in range(0, source.width, 2):
            _luma, cb, cr = cast(tuple[int, int, int], skin_pixels[x, y])
            if 77 <= cb <= 127 and 133 <= cr <= 173:
                count += 1
                first = cast(tuple[int, int, int], source_pixels[x, y])
                second = cast(tuple[int, int, int], target_pixels[x, y])
                error += sum(abs(int(first[channel]) - int(second[channel])) for channel in range(3)) / 3
    if count < 20:
        return 100.0, count
    mean_error = error / count
    return max(0.0, 100.0 - mean_error / 1.5), count


def evaluate_e6_suitability(image: Image.Image) -> E6Suitability:
    sample = ImageOps.exif_transpose(image).convert("RGB")
    sample.thumbnail((112, 112), Image.Resampling.LANCZOS)
    preview = encode_image(
        sample,
        profile_key="gdep073e01_6c",
        dither="bayer4",
        color_distance="oklab",
        strength=0.75,
    ).preview
    original_contrast = float(ImageStat.Stat(sample.convert("L")).stddev[0])
    preview_contrast = float(ImageStat.Stat(preview.convert("L")).stddev[0])
    contrast_score = 100.0 if original_contrast < 4 else min(
        100.0, preview_contrast / original_contrast * 100.0
    )
    original_edges = _edge_energy(sample)
    preview_edges = _edge_energy(preview)
    detail_score = 100.0 if original_edges < 2 else min(100.0, preview_edges / original_edges * 100.0)
    subject_score = contrast_score * 0.55 + detail_score * 0.45
    text_score = _strong_edge_retention(sample, preview)
    skin_score, skin_pixels = _skin_fidelity(sample, preview)
    score = (
        contrast_score * 0.25
        + subject_score * 0.35
        + skin_score * 0.20
        + text_score * 0.20
    )
    return E6Suitability(
        score=round(_clamp(score / 100.0) * 100.0, 2),
        contrast_score=round(_clamp(contrast_score / 100.0) * 100.0, 2),
        subject_score=round(_clamp(subject_score / 100.0) * 100.0, 2),
        skin_score=round(_clamp(skin_score / 100.0) * 100.0, 2),
        text_score=round(_clamp(text_score / 100.0) * 100.0, 2),
        skin_pixels=skin_pixels,
    )
