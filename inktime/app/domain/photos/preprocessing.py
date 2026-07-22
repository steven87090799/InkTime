from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, ImageOps, ImageStat

from inktime.app.domain.rendering.composition import (
    analyze_crop_focus,
)

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover - requirements 正式環境會安裝，保留最小匯入能力。
    pass


_DCT_COS = tuple(
    tuple(math.cos((2 * position + 1) * frequency * math.pi / 64) for position in range(32))
    for frequency in range(8)
)


@dataclass(frozen=True)
class LocalPhotoFeatures:
    sha256: str
    perceptual_hash: str | None
    difference_hash: str | None
    width: int
    height: int
    format: str
    exif_json: str | None
    captured_at: str | None
    gps_lat: float | None
    gps_lon: float | None
    brightness: float | None
    contrast: float | None
    blur_score: float | None
    overexposed_ratio: float | None
    underexposed_ratio: float | None
    screenshot_likelihood: float | None
    crop_focus_x: float | None
    crop_focus_y: float | None
    crop_subject_left: float | None
    crop_subject_top: float | None
    crop_subject_right: float | None
    crop_subject_bottom: float | None
    crop_method: str | None
    crop_face_count: int | None
    e6_score: float | None
    e6_contrast_score: float | None
    e6_subject_score: float | None
    e6_skin_score: float | None
    e6_text_score: float | None
    e6_skin_pixels: int | None
    metadata_complete: bool = True
    local_features_complete: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


def _bits_to_hex(bits: list[bool]) -> str:
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:0{len(bits) // 4}x}"


def _dhash(image: Image.Image) -> str:
    sample = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(sample.getdata())
    bits = [pixels[y * 9 + x] > pixels[y * 9 + x + 1] for y in range(8) for x in range(8)]
    return _bits_to_hex(bits)


def _phash(image: Image.Image) -> str:
    sample = image.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
    pixels = [float(value) for value in sample.getdata()]  # type: ignore[attr-defined]
    coefficients: list[float] = []
    # 以可分離 DCT 取代每個係數重算 32×32 次三角函數；結果等價但 CPU 工作量更低。
    x_projection = [
        [
            sum(pixels[y * 32 + x] * _DCT_COS[u][x] for x in range(32))
            for y in range(32)
        ]
        for u in range(8)
    ]
    for u in range(8):
        for v in range(8):
            coefficients.append(
                sum(x_projection[u][y] * _DCT_COS[v][y] for y in range(32))
            )
    median = sorted(coefficients[1:])[len(coefficients[1:]) // 2]
    return _bits_to_hex([value > median for value in coefficients])


def _blur_variance(image: Image.Image) -> float:
    sample = image.convert("L")
    sample.thumbnail((256, 256))
    width, height = sample.size
    if width < 3 or height < 3:
        return 0.0
    pixels: Any = sample.load()
    count = 0
    mean = 0.0
    squared_delta = 0.0
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            value = (
                4 * pixels[x, y]
                - pixels[x - 1, y]
                - pixels[x + 1, y]
                - pixels[x, y - 1]
                - pixels[x, y + 1]
            )
            count += 1
            delta = value - mean
            mean += delta / count
            squared_delta += delta * (value - mean)
    return squared_delta / count if count else 0.0


def _rational(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _gps_coordinate(values, reference: str | None) -> float | None:
    if not values or len(values) != 3:
        return None
    result = _rational(values[0]) + _rational(values[1]) / 60 + _rational(values[2]) / 3600
    return -result if reference in {"S", "W"} else result


class PhotoPreprocessor:
    def analyze(
        self,
        path: Path,
        *,
        include_metadata: bool = True,
        include_local_features: bool = True,
    ) -> LocalPhotoFeatures:
        """一次讀檔，可依掃描模式只做 Metadata 或本地影像特徵。

        SHA-256、格式與尺寸永遠取得，因為它們是搬移／重複辨識與內容變更
        失效判斷的最低安全資料。
        """
        digest = sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        with Image.open(path) as opened:
            original_format = opened.format or path.suffix.lstrip(".").upper()
            original_width, original_height = opened.size
            exif = opened.getexif()
            exif_named = {
                ExifTags.TAGS.get(key, str(key)): value for key, value in exif.items() if key != 34853
            }
            try:
                exif_detail = exif.get_ifd(34665)
            except (AttributeError, KeyError, TypeError):
                exif_detail = {}
            for key, value in exif_detail.items():
                exif_named.setdefault(ExifTags.TAGS.get(key, str(key)), value)
            gps_raw = exif.get_ifd(34853) if include_metadata and exif and 34853 in exif else {}
            gps = {ExifTags.GPSTAGS.get(key, str(key)): value for key, value in gps_raw.items()}
            lat = (
                _gps_coordinate(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
                if include_metadata
                else None
            )
            lon = (
                _gps_coordinate(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
                if include_metadata
                else None
            )
            captured = (
                exif_named.get("DateTimeOriginal") or exif_named.get("DateTime")
                if include_metadata
                else None
            )
            captured_at = None
            if captured:
                try:
                    captured_at = datetime.strptime(str(captured), "%Y:%m:%d %H:%M:%S").isoformat()
                except ValueError:
                    captured_at = None
            orientation = int(exif.get(274, 1) or 1)
            width, height = (
                (original_height, original_width)
                if orientation in {5, 6, 7, 8}
                else (original_width, original_height)
            )
            serializable_exif = (
                {key: str(value) for key, value in exif_named.items()} if include_metadata else None
            )
            if serializable_exif is not None and lat is not None and lon is not None:
                serializable_exif["gps"] = "[已擷取；診斷包會遮蔽精確座標]"

            perceptual_hash = difference_hash = None
            brightness = contrast = blur_score = None
            overexposed_ratio = underexposed_ratio = screenshot_likelihood = None
            crop_focus_x = crop_focus_y = None
            crop_subject_left = crop_subject_top = None
            crop_subject_right = crop_subject_bottom = None
            crop_method = None
            crop_face_count = None
            if include_local_features:
                # 所有品質特徵只需要小樣本。先要求 JPEG decoder 降採樣，再限制到 512px，
                # 避免 24MP／48MP 原始圖在每個並行槽展開成數十至數百 MiB。
                opened.draft("RGB", (512, 512))
                opened.thumbnail((512, 512), Image.Resampling.LANCZOS)
                image = ImageOps.exif_transpose(opened).convert("RGB")
                grayscale = image.convert("L")
                crop = analyze_crop_focus(image)
                stat = ImageStat.Stat(grayscale)
                histogram = grayscale.histogram()
                total_pixels = max(1, grayscale.width * grayscale.height)
                common_screen_sizes = {
                    (750, 1334),
                    (828, 1792),
                    (1080, 1920),
                    (1080, 2340),
                    (1125, 2436),
                    (1170, 2532),
                    (1179, 2556),
                    (1242, 2688),
                    (1284, 2778),
                    (1290, 2796),
                    (1440, 2560),
                    (1920, 1080),
                    (2048, 2732),
                }
                normalized_size = (min(width, height), max(width, height))
                normalized_screen_sizes = {
                    (min(screen_width, screen_height), max(screen_width, screen_height))
                    for screen_width, screen_height in common_screen_sizes
                }
                exact_screen_size = normalized_size in normalized_screen_sizes
                ratio = min(width, height) / max(width, height) if width and height else 0
                screen_ratios = {
                    min(screen_width, screen_height) / max(screen_width, screen_height)
                    for screen_width, screen_height in common_screen_sizes
                }
                screen_ratio = any(abs(ratio - candidate) < 0.004 for candidate in screen_ratios)
                filename = path.name.casefold()
                software = str(exif_named.get("Software", "")).casefold()
                filename_match = any(
                    marker in filename
                    for marker in ("screenshot", "screen shot", "截圖", "螢幕快照")
                )
                software_match = any(
                    marker in software for marker in ("screenshot", "screen capture")
                )
                perceptual_hash = _phash(image)
                difference_hash = _dhash(image)
                brightness = float(stat.mean[0])
                contrast = float(stat.stddev[0])
                blur_score = float(_blur_variance(grayscale))
                overexposed_ratio = sum(histogram[245:]) / total_pixels
                underexposed_ratio = sum(histogram[:11]) / total_pixels
                screenshot_likelihood = min(
                    1.0,
                    (0.85 if filename_match else 0)
                    + (0.75 if software_match else 0)
                    + (0.65 if exact_screen_size else 0)
                    + (0.3 if screen_ratio and not exact_screen_size else 0)
                    + (0.25 if not exif_named.get("Make") else 0)
                    + (0.15 if original_format.upper() == "PNG" else 0),
                )
                crop_focus_x = crop.focus_x
                crop_focus_y = crop.focus_y
                crop_subject_left = crop.subject_left
                crop_subject_top = crop.subject_top
                crop_subject_right = crop.subject_right
                crop_subject_bottom = crop.subject_bottom
                crop_method = crop.method
                crop_face_count = crop.face_count
            return LocalPhotoFeatures(
                sha256=digest.hexdigest(),
                perceptual_hash=perceptual_hash,
                difference_hash=difference_hash,
                width=width,
                height=height,
                format=original_format,
                exif_json=(
                    json.dumps(serializable_exif, ensure_ascii=False)
                    if serializable_exif is not None
                    else None
                ),
                captured_at=captured_at,
                gps_lat=lat,
                gps_lon=lon,
                brightness=brightness,
                contrast=contrast,
                blur_score=blur_score,
                overexposed_ratio=overexposed_ratio,
                underexposed_ratio=underexposed_ratio,
                screenshot_likelihood=screenshot_likelihood,
                crop_focus_x=crop_focus_x,
                crop_focus_y=crop_focus_y,
                crop_subject_left=crop_subject_left,
                crop_subject_top=crop_subject_top,
                crop_subject_right=crop_subject_right,
                crop_subject_bottom=crop_subject_bottom,
                crop_method=crop_method,
                crop_face_count=crop_face_count,
                e6_score=None,
                e6_contrast_score=None,
                e6_subject_score=None,
                e6_skin_score=None,
                e6_text_score=None,
                e6_skin_pixels=0 if include_local_features else None,
                metadata_complete=include_metadata,
                local_features_complete=include_local_features,
            )
