from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, ImageOps, ImageStat

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
    perceptual_hash: str
    difference_hash: str
    width: int
    height: int
    format: str
    exif_json: str
    captured_at: str | None
    gps_lat: float | None
    gps_lon: float | None
    brightness: float
    contrast: float
    blur_score: float
    overexposed_ratio: float
    underexposed_ratio: float
    screenshot_likelihood: float

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
    def analyze(self, path: Path) -> LocalPhotoFeatures:
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
            gps_raw = exif.get_ifd(34853) if exif and 34853 in exif else {}
            gps = {ExifTags.GPSTAGS.get(key, str(key)): value for key, value in gps_raw.items()}
            lat = _gps_coordinate(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
            lon = _gps_coordinate(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
            captured = exif_named.get("DateTimeOriginal") or exif_named.get("DateTime")
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
            # 所有品質特徵只需要小樣本。先要求 JPEG decoder 降採樣，再限制到 512px，
            # 避免 24MP／48MP 原始圖在每個並行槽展開成數十至數百 MiB。
            opened.draft("RGB", (512, 512))
            opened.thumbnail((512, 512), Image.Resampling.LANCZOS)
            image = ImageOps.exif_transpose(opened).convert("RGB")
            grayscale = image.convert("L")
            sample = grayscale
            stat = ImageStat.Stat(sample)
            histogram = sample.histogram()
            total_pixels = max(1, sample.width * sample.height)
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
                marker in filename for marker in ("screenshot", "screen shot", "截圖", "螢幕快照")
            )
            software_match = any(marker in software for marker in ("screenshot", "screen capture"))
            screenshot_likelihood = min(
                1.0,
                (0.85 if filename_match else 0)
                + (0.75 if software_match else 0)
                + (0.65 if exact_screen_size else 0)
                + (0.3 if screen_ratio and not exact_screen_size else 0)
                + (0.25 if not exif_named.get("Make") else 0)
                + (0.15 if original_format.upper() == "PNG" else 0),
            )
            serializable_exif = {key: str(value) for key, value in exif_named.items()}
            if lat is not None and lon is not None:
                serializable_exif["gps"] = "[已擷取；診斷包會遮蔽精確座標]"
            return LocalPhotoFeatures(
                sha256=digest.hexdigest(),
                perceptual_hash=_phash(image),
                difference_hash=_dhash(image),
                width=width,
                height=height,
                format=original_format,
                exif_json=json.dumps(serializable_exif, ensure_ascii=False),
                captured_at=captured_at,
                gps_lat=lat,
                gps_lon=lon,
                brightness=float(stat.mean[0]),
                contrast=float(stat.stddev[0]),
                blur_score=float(_blur_variance(grayscale)),
                overexposed_ratio=sum(histogram[245:]) / total_pixels,
                underexposed_ratio=sum(histogram[:11]) / total_pixels,
                screenshot_likelihood=screenshot_likelihood,
            )
