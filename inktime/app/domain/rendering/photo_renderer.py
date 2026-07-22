from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .palette import EncodedImage, encode_image, palette_for_profile


BUILTIN_PHOTO_PRESETS: dict[str, dict[str, Any]] = {
    "photo_balanced": {
        "label": "照片平衡",
        "denoise": True,
        "exif_orientation": True,
        "high_quality_resize": True,
        "denoise_strength": 0.35,
        "tone_mapping": True,
        "tone_strength": 0.35,
        "local_contrast": True,
        "contrast_strength": 0.30,
        "sharpen": True,
        "sharpen_strength": 0.35,
        "dither": "serpentine_floyd_steinberg",
        "error_strength": 0.85,
        "color_distance": "oklab",
        "linear_light": True,
    },
    "portrait_clear": {
        "label": "人像清晰",
        "denoise": True,
        "exif_orientation": True,
        "high_quality_resize": True,
        "denoise_strength": 0.55,
        "tone_mapping": True,
        "tone_strength": 0.25,
        "local_contrast": True,
        "contrast_strength": 0.18,
        "sharpen": True,
        "sharpen_strength": 0.22,
        "dither": "serpentine_floyd_steinberg",
        "error_strength": 0.72,
        "color_distance": "oklab",
        "linear_light": True,
        "protect_faces": True,
    },
    "landscape_smooth": {
        "label": "風景平滑",
        "denoise": True,
        "exif_orientation": True,
        "high_quality_resize": True,
        "denoise_strength": 0.42,
        "tone_mapping": True,
        "tone_strength": 0.48,
        "local_contrast": True,
        "contrast_strength": 0.40,
        "sharpen": True,
        "sharpen_strength": 0.28,
        "dither": "serpentine_floyd_steinberg",
        "error_strength": 0.90,
        "color_distance": "oklab",
        "linear_light": True,
    },
    "text_graphic": {
        "label": "文字圖像",
        "denoise": False,
        "exif_orientation": True,
        "high_quality_resize": True,
        "denoise_strength": 0.0,
        "tone_mapping": True,
        "tone_strength": 0.20,
        "local_contrast": True,
        "contrast_strength": 0.50,
        "sharpen": True,
        "sharpen_strength": 0.70,
        "dither": "bayer_ordered",
        "error_strength": 0.55,
        "color_distance": "oklab",
        "linear_light": True,
        "protect_text": True,
    },
    "maximum_clarity": {
        "label": "最高清晰度",
        "denoise": True,
        "exif_orientation": True,
        "high_quality_resize": True,
        "denoise_strength": 0.20,
        "tone_mapping": True,
        "tone_strength": 0.58,
        "local_contrast": True,
        "contrast_strength": 0.62,
        "sharpen": True,
        "sharpen_strength": 0.78,
        "dither": "serpentine_floyd_steinberg",
        "error_strength": 1.0,
        "color_distance": "oklab",
        "linear_light": True,
    },
}

PIPELINE_BOOLEAN_FIELDS = {
    "denoise",
    "tone_mapping",
    "local_contrast",
    "sharpen",
    "linear_light",
    "protect_text",
    "protect_faces",
    "exif_orientation",
    "high_quality_resize",
}
PIPELINE_STRENGTH_FIELDS = {
    "denoise_strength",
    "tone_strength",
    "contrast_strength",
    "sharpen_strength",
    "error_strength",
}


@dataclass(frozen=True)
class PhotoRenderResult:
    source: Image.Image
    processed: Image.Image
    encoded: EncodedImage
    render_ms: int
    preset: str
    options: dict[str, Any]
    protected_mask: Image.Image | None


def resolve_photo_options(preset: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    if preset not in BUILTIN_PHOTO_PRESETS:
        raise ValueError(f"RENDER-007 找不到照片 Preset：{preset}")
    options = dict(BUILTIN_PHOTO_PRESETS[preset])
    options.pop("label", None)
    for key, value in (overrides or {}).items():
        if key in PIPELINE_BOOLEAN_FIELDS:
            options[key] = value is True or str(value).lower() in {"1", "true", "on", "yes"}
        elif key in PIPELINE_STRENGTH_FIELDS:
            parsed = float(value)
            if not 0 <= parsed <= 2:
                raise ValueError(f"RENDER-007 {key} 必須介於 0 到 2")
            options[key] = parsed
        elif key in {"dither", "color_distance"}:
            options[key] = str(value)
    return options


def prepare_photo_canvas(
    image: Image.Image,
    *,
    size: tuple[int, int] = (480, 800),
    fit: str = "cover",
    apply_exif: bool = True,
    high_quality: bool = True,
) -> Image.Image:
    if fit not in {"cover", "contain"}:
        raise ValueError("RENDER-007 圖片縮放模式不合法")
    source = ImageOps.exif_transpose(image) if apply_exif else image.copy()
    source = source.convert("RGB")
    resampling = Image.Resampling.LANCZOS if high_quality else Image.Resampling.BICUBIC
    if fit == "cover":
        return ImageOps.fit(source, size, method=resampling)
    fitted = ImageOps.contain(source, size, method=resampling)
    canvas = Image.new("RGB", size, "white")
    canvas.paste(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2))
    return canvas


def _blend(original: Image.Image, adjusted: Image.Image, strength: float) -> Image.Image:
    return Image.blend(original, adjusted, max(0.0, min(1.0, strength)))


def _luminance_sharpen(image: Image.Image, strength: float) -> Image.Image:
    y_channel, cb, cr = image.convert("YCbCr").split()
    sharpened = y_channel.filter(
        ImageFilter.UnsharpMask(radius=1.25, percent=int(60 + 170 * strength), threshold=2)
    )
    blended = Image.blend(y_channel, sharpened, max(0.0, min(1.0, strength)))
    return Image.merge("YCbCr", (blended, cb, cr)).convert("RGB")


def _region_mask(size: tuple[int, int], regions: list[list[float]] | None) -> Image.Image | None:
    if not regions:
        return None
    from PIL import ImageDraw

    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for region in regions[:100]:
        if len(region) != 4:
            continue
        left, top, right, bottom = (float(value) for value in region)
        if max(abs(value) for value in (left, top, right, bottom)) <= 1:
            left, right = left * size[0], right * size[0]
            top, bottom = top * size[1], bottom * size[1]
        draw.rectangle(
            (
                max(0, min(size[0], int(left))),
                max(0, min(size[1], int(top))),
                max(0, min(size[0], int(right))),
                max(0, min(size[1], int(bottom))),
            ),
            fill=255,
        )
    return mask if mask.getbbox() else None


def render_photo(
    image: Image.Image,
    *,
    profile_key: str,
    preset: str = "photo_balanced",
    overrides: dict[str, Any] | None = None,
    fit: str = "cover",
    palette_rgb: dict[str, list[int]] | None = None,
    palette_lab: dict[str, list[float]] | None = None,
    palette_version: str = "custom-1",
    text_regions: list[list[float]] | None = None,
    face_regions: list[list[float]] | None = None,
) -> PhotoRenderResult:
    started = perf_counter()
    options = resolve_photo_options(preset, overrides)
    source = prepare_photo_canvas(
        image,
        fit=fit,
        apply_exif=bool(options.get("exif_orientation", True)),
        high_quality=bool(options.get("high_quality_resize", True)),
    )
    processed = source.copy()

    if options.get("denoise") and float(options.get("denoise_strength", 0)) > 0:
        denoised = processed.filter(ImageFilter.MedianFilter(size=3))
        processed = _blend(processed, denoised, min(1, float(options["denoise_strength"])))
    if options.get("tone_mapping") and float(options.get("tone_strength", 0)) > 0:
        mapped = ImageOps.autocontrast(processed, cutoff=1, preserve_tone=True)
        mapped = ImageEnhance.Brightness(mapped).enhance(1.02)
        processed = _blend(processed, mapped, min(1, float(options["tone_strength"])))
    if options.get("local_contrast") and float(options.get("contrast_strength", 0)) > 0:
        contrast = processed.filter(
            ImageFilter.UnsharpMask(
                radius=8,
                percent=int(45 + float(options["contrast_strength"]) * 100),
                threshold=4,
            )
        )
        processed = _blend(processed, contrast, min(1, float(options["contrast_strength"])))
    if options.get("sharpen") and float(options.get("sharpen_strength", 0)) > 0:
        sharpened = _luminance_sharpen(processed, float(options["sharpen_strength"]))
        if options.get("protect_faces"):
            face_mask = _region_mask(processed.size, face_regions)
            if face_mask is not None:
                softened = _blend(processed, sharpened, 0.25)
                sharpened = Image.composite(softened, sharpened, face_mask)
        processed = sharpened

    text_mask = _region_mask(processed.size, text_regions) if options.get("protect_text") else None
    profile = palette_for_profile(
        profile_key,
        rgb_values=palette_rgb,
        lab_values=palette_lab,
        palette_version=palette_version,
    )
    encoded = encode_image(
        processed,
        profile_key=profile_key,
        profile=profile,
        dither=str(options["dither"]),
        color_distance=str(options["color_distance"]),
        strength=float(options["error_strength"]),
        linear_light=bool(options.get("linear_light", True)),
        protected_mask=text_mask,
    )
    return PhotoRenderResult(
        source=source,
        processed=processed,
        encoded=encoded,
        render_ms=int((perf_counter() - started) * 1000),
        preset=preset,
        options=options,
        protected_mask=text_mask,
    )
