"""Source image contract. Originals are read-only; only bounded RGB leaves here.

Pillow HEIF 1.5 has no reduced-resolution decode API. Serialize source opens
across processes (including codec header/metadata allocation), shrink before
transpose/conversion, and keep libheif's security limits enabled. JPEG uses
decoder draft reduction. This does NOT claim thumbnail() bounds HEIF decoding.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
from hashlib import sha256
import logging
import os
from pathlib import Path
import stat
import tempfile
import threading

from PIL import Image, ImageOps, UnidentifiedImageError

from inktime.app.core.locks import FcntlLockProvider, LockUnavailableError
from inktime.app.core.logging import log_event, should_log_rate_limited


SUPPORTED_IMAGE_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".heic",
        ".heif",
        ".tif",
        ".tiff",
        ".bmp",
    }
)
SUPPORTED_PIL_FORMATS = ("JPEG", "PNG", "WEBP", "HEIF", "TIFF", "BMP")
HEIF_EXTENSIONS = frozenset({".heic", ".heif"})
UNSUPPORTED_RAW_EXTENSIONS = frozenset({".dng"})
DERIVATIVE_FORMAT = "JPEG"
DERIVATIVE_MEDIA_TYPE = "image/jpeg"
DERIVATIVE_SIZES = frozenset({512, 1024, 1600})
DERIVATIVE_VERSION = "v2"
MAX_SOURCE_PIXELS = 60_000_000
MAX_SOURCE_EDGE = 12_000
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_WORKING_EDGE = 3200
_codec_lock = threading.Lock()
_heif_available: bool | None = None
LOGGER = logging.getLogger("images")


class ImageSourceError(OSError):
    """Safe, stable diagnostic; never contains a source path or codec message."""

    def __init__(self, code: str, message: str, status: int = 422):
        self.code = code
        self.status = status
        self.retryable = status == 503 and code != "IMG-HEIF-UNAVAILABLE"
        super().__init__(f"{code} {message}")


def is_supported_image_path(path: Path | str) -> bool:
    return Path(path).suffix.casefold() in SUPPORTED_IMAGE_EXTENSIONS


def ensure_image_codecs_registered() -> bool:
    global _heif_available
    with _codec_lock:
        if _heif_available is None:
            try:
                from pillow_heif import libheif_info, register_heif_opener

                register_heif_opener(decode_threads=1, thumbnails=False, depth_images=False, aux_images=False)
                _heif_available = any(
                    "HEVC" in str(value) for value in libheif_info().get("decoders", {}).values()
                )
            except (ImportError, OSError, RuntimeError):
                _heif_available = False
        return _heif_available


def image_capabilities() -> dict:
    return {
        "supported_extensions": sorted(SUPPORTED_IMAGE_EXTENSIONS),
        "heif_decoder_available": ensure_image_codecs_registered(),
        "dng": "unsupported: iPhone ProRAW .DNG 尚未支援",
        "gif": "excluded/video-like",
        "derivative_format": DERIVATIVE_FORMAT,
        "max_pixels": MAX_SOURCE_PIXELS,
        "max_edge": MAX_SOURCE_EDGE,
        "max_file_bytes": MAX_FILE_BYTES,
    }


def validate_dimensions(width: int, height: int, *, max_pixels=MAX_SOURCE_PIXELS, max_edge=MAX_SOURCE_EDGE):
    if (
        min(width, height) <= 0
        or width * height > min(max_pixels, MAX_SOURCE_PIXELS)
        or max(width, height) > min(max_edge, MAX_SOURCE_EDGE)
    ):
        raise ImageSourceError("THUMB-005", "原始照片尺寸超過圖片安全上限", 413)


def validate_source_file(path: Path):
    if not is_supported_image_path(path):
        raise ImageSourceError("IMG-UNSUPPORTED", "照片格式尚未支援（包含 iPhone ProRAW DNG）", 415)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ImageSourceError("IMG-IO", "照片不是可讀取的一般檔案", 403)
    if info.st_size > MAX_FILE_BYTES:
        raise ImageSourceError("IMG-FILE-LIMIT", "原始照片檔案超過圖片安全上限", 413)
    return info


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def safe_image_open(path: Path, *, max_pixels=MAX_SOURCE_PIXELS, max_edge=MAX_SOURCE_EDGE, hash_source=False):
    """Hold the shared source-decode slot until the decoder is closed.

    Production processes share /data. Non-container tools share a per-user
    temporary lock. Never create locks or derivatives in the source library.
    """
    try:
        before = validate_source_file(path)
        available = ensure_image_codecs_registered()
        if path.suffix.casefold() in HEIF_EXTENSIONS and not available:
            raise ImageSourceError("IMG-HEIF-UNAVAILABLE", "HEIF 解碼器未安裝或不可用", 503)
        lock_dir = Path(os.environ.get("INKTIME_DATA_DIR") or tempfile.gettempdir())
        # Private parent prevents another local UID from planting a symlink at
        # the predictable lock filename (notably for command-line /tmp use).
        private_dir = lock_dir / f".inktime-image-decode-{os.getuid()}"
        private_dir.mkdir(mode=0o700, exist_ok=True)
        lock_stat = private_dir.lstat()
        if (
            not stat.S_ISDIR(lock_stat.st_mode)
            or lock_stat.st_uid != os.getuid()
            or stat.S_IMODE(lock_stat.st_mode) & 0o077
        ):
            raise ImageSourceError("IMG-IO", "圖片解碼鎖目錄不安全", 503)
        lock_path = private_dir / "source.lock"
        with FcntlLockProvider().exclusive(lock_path, timeout_seconds=30):
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                if _signature(os.fstat(stream.fileno())) != _signature(before):
                    raise ImageSourceError("THUMB-004", "原始照片內容已在圖片建立期間改變", 409)
                formats = [fmt for fmt in SUPPORTED_PIL_FORMATS if fmt != "HEIF" or available]
                with Image.open(stream, formats=formats) as opened:
                    validate_dimensions(*opened.size, max_pixels=max_pixels, max_edge=max_edge)
                    if hash_source:
                        # Hash the same read-only descriptor as metadata/decode,
                        # not a second path open that could race a replacement.
                        position = stream.tell()
                        stream.seek(0)
                        digest = sha256()
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                        stream.seek(position)
                        opened.info["inktime_source_sha256"] = digest.hexdigest()
                    yield opened
                if _signature(path.stat()) != _signature(before):
                    raise ImageSourceError("THUMB-004", "原始照片內容已在圖片建立期間改變", 409)
    except ImageSourceError:
        raise
    except FileNotFoundError as exc:
        raise ImageSourceError("IMG-MISSING", "原始照片不存在", 404) from exc
    except PermissionError as exc:
        raise ImageSourceError("IMG-IO", "無法讀取原始照片", 403) from exc
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageSourceError("THUMB-005", "原始照片尺寸超過圖片安全上限", 413) from exc
    except LockUnavailableError as exc:
        raise ImageSourceError("IMG-DECODE-BUSY", "圖片解碼忙碌，請稍後重試", 503) from exc
    except (UnidentifiedImageError, SyntaxError, EOFError, ValueError) as exc:
        raise ImageSourceError("IMG-CORRUPT", "圖片無法解碼或內容已損壞") from exc
    except OSError as exc:
        if exc.errno in {
            errno.EACCES,
            errno.EPERM,
            errno.EIO,
            errno.ELOOP,
            errno.EEXIST,
            errno.ENOTDIR,
            errno.ENOSPC,
            errno.EROFS,
        }:
            raise ImageSourceError("IMG-IO", "無法讀取原始照片", 503) from exc
        raise ImageSourceError("IMG-CORRUPT", "圖片無法解碼或內容已損壞") from exc


def bounded_rgb(opened: Image.Image, max_side: int | tuple[int, int]) -> Image.Image:
    """Consume a source inside safe_image_open; return an independent small RGB.

    libheif applies container transforms and clears EXIF Orientation itself.
    Pillow handles JPEG/TIFF EXIF; never reapply HEIF original_orientation.
    """
    requested = max_side if isinstance(max_side, tuple) else (max_side, max_side)
    bounds = (
        max(1, min(int(requested[0]), MAX_WORKING_EDGE)),
        max(1, min(int(requested[1]), MAX_WORKING_EDGE)),
    )
    opened.draft("RGB", bounds)
    # Non-JPEG codecs still decode fully here, under the single shared slot.
    # Reduce BEFORE any full-size transpose, RGBA/RGB conversion, or copy.
    opened.thumbnail(bounds, Image.Resampling.LANCZOS)
    validate_dimensions(*opened.size)
    oriented = ImageOps.exif_transpose(opened)
    try:
        oriented.thumbnail(bounds, Image.Resampling.LANCZOS)
        if oriented.mode in {"RGBA", "LA", "P", "PA"}:
            with oriented.convert("RGBA") as rgba:
                result = Image.new("RGB", rgba.size, "white")
                result.paste(rgba, mask=rgba.getchannel("A"))
        elif oriented.mode.startswith("I;16") or oriented.mode == "I":
            # Deterministic 16-bit grayscale -> 8-bit, not Pillow's clipping.
            result = oriented.convert("I").point(lambda value: value / 256).convert("RGB")
        else:
            result = oriented.convert("RGB")
        result.info.clear()  # no stale EXIF/XMP orientation or GPS in derivatives
        return result
    finally:
        oriented.close()


def load_rgb(path: Path, max_side: int | tuple[int, int] = 1600) -> Image.Image:
    with safe_image_open(path) as opened:
        return bounded_rgb(opened, max_side)


def log_image_error(exc: ImageSourceError, *, photo_id: str, stage: str, suffix: str) -> None:
    event = (
        "heif_decoder_unavailable"
        if exc.code == "IMG-HEIF-UNAVAILABLE"
        else "image_safety_rejected"
        if exc.status == 413
        else "image_decode_failed"
        if stage == "preprocess"
        else f"{stage}_generation_failed"
    )
    # Bound cardinality and rate globally per stage/code, not per private path.
    if should_log_rate_limited(f"image:{stage}:{exc.code}", interval_seconds=60):
        log_event(
            LOGGER,
            logging.WARNING,
            "圖片處理失敗",
            event=event,
            error_code=exc.code,
            photo_id=photo_id,
            operation=stage,
            retryable=exc.retryable,
            details={"stage": stage, "format": suffix[:12]},
        )
