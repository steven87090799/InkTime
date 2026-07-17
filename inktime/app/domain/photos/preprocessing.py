from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, ImageOps, ImageStat


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
    pixels = [float(value) for value in sample.getdata()]
    coefficients: list[float] = []
    for u in range(8):
        for v in range(8):
            total = 0.0
            for x in range(32):
                cx = math.cos((2 * x + 1) * u * math.pi / 64)
                for y in range(32):
                    total += pixels[y * 32 + x] * cx * math.cos((2 * y + 1) * v * math.pi / 64)
            coefficients.append(total)
    median = sorted(coefficients[1:])[len(coefficients[1:]) // 2]
    return _bits_to_hex([value > median for value in coefficients])


def _blur_variance(image: Image.Image) -> float:
    sample = image.convert("L")
    sample.thumbnail((256, 256))
    width, height = sample.size
    if width < 3 or height < 3:
        return 0.0
    pixels = sample.load()
    values = []
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            values.append(4 * pixels[x, y] - pixels[x - 1, y] - pixels[x + 1, y] - pixels[x, y - 1] - pixels[x, y + 1])
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / len(values)


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
            exif = opened.getexif()
            exif_named = {ExifTags.TAGS.get(key, str(key)): value for key, value in exif.items() if key != 34853}
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
            image = ImageOps.exif_transpose(opened).convert("RGB")
            width, height = image.size
            grayscale = image.convert("L")
            sample = grayscale.copy()
            sample.thumbnail((512, 512))
            stat = ImageStat.Stat(sample)
            histogram = sample.histogram()
            total_pixels = max(1, sample.width * sample.height)
            common_ratios = {(1170, 2532), (1080, 1920), (1242, 2688), (1440, 2560), (1080, 2340)}
            exact_screen_ratio = any(abs(width / height - w / h) < 0.006 for w, h in common_ratios) if height else False
            screenshot_likelihood = min(1.0, (0.65 if exact_screen_ratio else 0) + (0.2 if not exif_named.get("Make") else 0) + (0.15 if path.name.lower().startswith(("screenshot", "截圖")) else 0))
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
