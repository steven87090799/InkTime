from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import time
from typing import Callable, Iterable, Iterator, Sequence

from inktime.app.domain.photos import PhotoPreprocessor, ThumbnailCache
from inktime.app.repositories.photos import (
    BatchPhotoResult,
    PhotoRepository,
    PreparedScanPhoto,
    StoredPhotoSignature,
)


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp"}
VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".gif",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mts",
    ".webm",
    ".wmv",
}
SCAN_MODES = {"incremental", "full", "metadata-only", "local-features-only", "manual"}
TRIGGER_SOURCES = {"manual", "api", "scheduler", "virtual-display", "test"}


@dataclass(frozen=True)
class DiskPhoto:
    path: Path
    relative_path: str
    file_size: int
    modified_time: float


def iter_media(
    root: Path, *, on_error: Callable[[OSError], None] | None = None
) -> Iterator[tuple[Path, str]]:
    """串流回傳媒體；walk 的權限／I/O 錯誤不得被靜默忽略。"""

    for directory, dirnames, filenames in os.walk(root, onerror=on_error):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for filename in filenames:
            path = Path(directory) / filename
            suffix = path.suffix.lower()
            if suffix in SUPPORTED_EXTENSIONS:
                yield path, "image"
            elif suffix in VIDEO_EXTENSIONS:
                yield path, "video"


def iter_images(root: Path) -> Iterator[Path]:
    """保留既有 generator API，不建立完整 100,000 筆路徑清單。"""

    for path, media_type in iter_media(root):
        if media_type == "image":
            yield path


def _batches(values: Iterable[tuple[Path, str]], size: int) -> Iterator[list[tuple[Path, str]]]:
    batch: list[tuple[Path, str]] = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _slices(values: Sequence, size: int) -> Iterator[Sequence]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _masked_path(relative_path: str) -> str:
    suffix = Path(relative_path).suffix.lower()[:12]
    return f"{sha256(relative_path.encode('utf-8', errors='replace')).hexdigest()[:16]}{suffix}"


def _scan_error(
    relative_path: str,
    *,
    stage: str,
    error_code: str,
    exc: BaseException,
    retryable: bool,
    photo_id: str | None = None,
) -> dict:
    return {
        "photo_id": photo_id,
        "stage": stage,
        "error_code": error_code,
        "exception_type": type(exc).__name__,
        "retryable": retryable,
        "masked_path": _masked_path(relative_path),
    }


class PhotoScanner:
    def __init__(
        self, repository: PhotoRepository, preprocessor: PhotoPreprocessor, thumbnails: ThumbnailCache
    ) -> None:
        self.repository = repository
        self.preprocessor = preprocessor
        self.thumbnails = thumbnails

    @staticmethod
    def _sections(
        mode: str, stored: StoredPhotoSignature | None, *, signature_changed: bool
    ) -> tuple[bool, bool]:
        force = mode == "full"
        wants_metadata = mode != "local-features-only"
        wants_local = mode != "metadata-only"
        if mode == "manual":
            wants_metadata = wants_local = True
        new_or_changed = stored is None or signature_changed or stored.lifecycle_status == "missing"
        metadata = wants_metadata and (
            force or new_or_changed or stored is None or stored.metadata_status != "complete"
        )
        local = wants_local and (
            force or new_or_changed or stored is None or stored.local_features_status != "complete"
        )
        return metadata, local

    def _analyze(self, entry: DiskPhoto, *, metadata: bool, local: bool):
        try:
            return self.preprocessor.analyze(
                entry.path,
                include_metadata=metadata,
                include_local_features=local,
            )
        except TypeError as exc:
            # 保留既有只接受 path 的測試／外掛 preprocessor；部分模式仍要求新版介面。
            if metadata and local and "unexpected keyword argument" in str(exc):
                return self.preprocessor.analyze(entry.path)
            raise

    def scan(
        self,
        name: str,
        root: Path,
        *,
        mode: str = "incremental",
        trigger_source: str = "manual",
        build_thumbnails: bool = True,
        limit: int | None = None,
        disk_batch_size: int = 1_000,
        write_batch_size: int = 500,
        missing_threshold_ratio: float = 0.10,
        cancel_requested: Callable[[], bool] | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        progress_interval_items: int = 50,
        progress_interval_seconds: int = 300,
    ) -> dict:
        if mode not in SCAN_MODES:
            raise ValueError("SCAN-003 不支援的掃描模式")
        if trigger_source not in TRIGGER_SOURCES:
            raise ValueError("SCAN-004 不支援的掃描來源")
        root = root.expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError("SCAN-001 照片資料夾不存在或無法讀取")
        try:
            with os.scandir(root) as entries:
                next(entries, None)
        except OSError as exc:
            raise OSError("SCAN-002 照片資料夾無法讀取") from exc

        disk_batch_size = max(100, min(int(disk_batch_size), 10_000))
        write_batch_size = max(100, min(int(write_batch_size), 2_000))
        cancel_requested = cancel_requested or (lambda: False)
        library_id = self.repository.ensure_library(name, root)
        scan_id = self.repository.begin_scan(
            library_id,
            root,
            mode=mode,
            trigger_source=trigger_source,
            missing_threshold_ratio=missing_threshold_ratio,
        )
        counts = {
            "checked": 0,
            "processed": 0,
            "skipped": 0,
            "new": 0,
            "changed": 0,
            "moved": 0,
            "restored": 0,
            "duplicates": 0,
            "inherited": 0,
            "failed": 0,
            "excluded_videos": 0,
        }
        last_progress_at = time.monotonic()
        major_io_errors = 0
        walk_errors: list[dict] = []
        cancelled = False
        full_census = False

        def on_walk_error(exc: OSError) -> None:
            nonlocal major_io_errors
            major_io_errors += 1
            relative = str(getattr(exc, "filename", "walk"))
            walk_errors.append(
                _scan_error(
                    relative,
                    stage="walk",
                    error_code="SCAN-IO-002",
                    exc=exc,
                    retryable=True,
                )
            )

        def report_progress(force: bool = False) -> None:
            nonlocal last_progress_at
            now = time.monotonic()
            if progress_callback and (
                force
                or counts["checked"] % max(1, progress_interval_items) == 0
                or now - last_progress_at >= max(1, progress_interval_seconds)
            ):
                progress_callback({"scan_id": scan_id, "mode": mode, **counts})
                last_progress_at = now

        if cancel_requested():
            cancelled = True
            media: Iterable[tuple[Path, str]] = ()
        else:
            media = iter_media(root, on_error=on_walk_error)
        for media_batch in _batches(media, disk_batch_size):
            if cancel_requested():
                cancelled = True
                break
            errors: list[dict] = []
            disk_entries: list[DiskPhoto] = []
            for path, media_type in media_batch:
                if cancel_requested():
                    cancelled = True
                    break
                if media_type == "video":
                    counts["excluded_videos"] += 1
                    continue
                if limit is not None and counts["processed"] + counts["failed"] >= limit:
                    cancelled = True
                    break
                counts["checked"] += 1
                try:
                    relative_path = path.relative_to(root).as_posix()
                    stat = path.stat()
                    disk_entries.append(
                        DiskPhoto(path, relative_path, int(stat.st_size), float(stat.st_mtime))
                    )
                except (OSError, ValueError) as exc:
                    # 無法 stat 的既有路徑不能被當作「完整且成功的磁碟 census」。
                    # 保留單張錯誤並繼續掃描，但整次 reconciliation 必須停用。
                    major_io_errors += 1
                    counts["failed"] += 1
                    errors.append(
                        _scan_error(
                            str(path),
                            stage="stat",
                            error_code="SCAN-IO-001",
                            exc=exc,
                            retryable=isinstance(exc, OSError),
                        )
                    )
            if cancelled:
                if errors:
                    self.repository.record_scan_errors(scan_id, errors)
                break
            signatures = self.repository.signatures_for_paths(
                library_id, [entry.relative_path for entry in disk_entries]
            )
            prepared: list[PreparedScanPhoto] = []
            classifications: dict[str, str] = {}
            seen_without_write: list[str] = []
            processing_failures: list[tuple[str, bool, bool]] = []

            for entry in disk_entries:
                stored = signatures.get(entry.relative_path)
                if stored and stored.lifecycle_status in {"excluded", "archived", "deleted"}:
                    seen_without_write.append(stored.id)
                    counts["skipped"] += 1
                    continue
                signature_changed = not bool(
                    stored
                    and stored.matches(
                        file_size=entry.file_size, modified_time=entry.modified_time
                    )
                )
                metadata, local = self._sections(
                    mode, stored, signature_changed=signature_changed
                )
                if not metadata and not local:
                    if stored is not None:
                        seen_without_write.append(stored.id)
                    counts["skipped"] += 1
                    continue
                if stored is None:
                    classification = "new"
                elif stored.lifecycle_status == "missing":
                    classification = "restored"
                elif signature_changed:
                    classification = "changed"
                else:
                    classification = "incomplete"
                try:
                    features = self._analyze(entry, metadata=metadata, local=local)
                    prepared.append(
                        PreparedScanPhoto(
                            entry.relative_path,
                            entry.path,
                            entry.file_size,
                            entry.modified_time,
                            features,
                        )
                    )
                    classifications[entry.relative_path] = classification
                except Exception as exc:
                    if stored is not None:
                        processing_failures.append((stored.id, metadata, local))
                    counts["failed"] += 1
                    errors.append(
                        _scan_error(
                            entry.relative_path,
                            stage="preprocess",
                            error_code="SCAN-PHOTO-001",
                            exc=exc,
                            retryable=isinstance(exc, OSError),
                            photo_id=stored.id if stored else None,
                        )
                    )

            for seen_chunk in _slices(seen_without_write, write_batch_size):
                self.repository.mark_seen_batch(scan_id, seen_chunk)
            for failed_chunk in _slices(processing_failures, write_batch_size):
                self.repository.mark_processing_failed_batch(scan_id, failed_chunk)

            for prepared_chunk in _slices(prepared, write_batch_size):
                try:
                    batch_results = self.repository.apply_scan_batch(
                        library_id, scan_id, root, prepared_chunk
                    )
                except Exception as exc:
                    major_io_errors += 1
                    counts["failed"] += len(prepared_chunk)
                    existing_ids = [
                        signatures[item.relative_path].id
                        for item in prepared_chunk
                        if item.relative_path in signatures
                    ]
                    if existing_ids:
                        self.repository.mark_seen_batch(scan_id, existing_ids)
                    errors.extend(
                        _scan_error(
                            item.relative_path,
                            stage="database",
                            error_code="SCAN-DB-001",
                            exc=exc,
                            retryable=True,
                            photo_id=(
                                signatures[item.relative_path].id
                                if item.relative_path in signatures
                                else None
                            ),
                        )
                        for item in prepared_chunk
                    )
                    continue
                by_path: dict[str, BatchPhotoResult] = {
                    result.relative_path: result for result in batch_results
                }
                for item in prepared_chunk:
                    result = by_path[item.relative_path]
                    classification = classifications[item.relative_path]
                    counts["processed"] += 1
                    if result.action == "moved":
                        counts["moved"] += 1
                    elif classification == "new":
                        counts["new"] += 1
                    elif classification == "changed":
                        counts["changed"] += 1
                    elif classification == "restored":
                        counts["restored"] += 1
                    counts["inherited"] += int(result.inherited)
                    counts["duplicates"] += int(result.inherited and result.action != "moved")
                    if build_thumbnails and result.action != "moved":
                        try:
                            self.thumbnails.get_or_create(item.source, result.sha256, 512)
                        except Exception as exc:
                            counts["failed"] += 1
                            errors.append(
                                _scan_error(
                                    item.relative_path,
                                    stage="thumbnail",
                                    error_code="THUMB-001",
                                    exc=exc,
                                    retryable=isinstance(exc, OSError),
                                    photo_id=result.photo_id,
                                )
                            )
            if errors:
                self.repository.record_scan_errors(scan_id, errors)
            if walk_errors:
                self.repository.record_scan_errors(scan_id, walk_errors)
                walk_errors = []
            report_progress()
        else:
            full_census = True

        if walk_errors:
            self.repository.record_scan_errors(scan_id, walk_errors)
        if cancel_requested():
            cancelled = True
        if limit is not None or cancelled:
            full_census = False
        scan = self.repository.finish_scan(
            scan_id,
            counts=counts,
            full_census=full_census,
            cancelled=cancelled,
            major_io_errors=major_io_errors,
        )
        report_progress(force=True)
        return {
            "library_id": library_id,
            "scan_id": scan_id,
            "mode": mode,
            **counts,
            "reconciliation_status": str(scan["reconciliation_status"]),
            "candidate_missing": int(scan["candidate_missing_count"]),
            "missing_marked": int(scan["missing_marked_count"]),
            "warning_code": scan["warning_code"],
            "cancelled": bool(scan["cancelled"]),
        }
