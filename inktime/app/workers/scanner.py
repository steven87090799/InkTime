from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Callable, Iterator

from inktime.app.domain.photos import PhotoPreprocessor, ThumbnailCache
from inktime.app.repositories.photos import PhotoRepository


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp"}


def iter_images(root: Path) -> Iterator[Path]:
    """以 generator 掃描，不建立完整 100,000 筆路徑清單。"""
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for filename in filenames:
            path = Path(directory) / filename
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                yield path


class PhotoScanner:
    def __init__(
        self, repository: PhotoRepository, preprocessor: PhotoPreprocessor, thumbnails: ThumbnailCache
    ) -> None:
        self.repository = repository
        self.preprocessor = preprocessor
        self.thumbnails = thumbnails

    def scan(
        self,
        name: str,
        root: Path,
        *,
        build_thumbnails: bool = True,
        limit: int | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        progress_interval_items: int = 50,
        progress_interval_seconds: int = 300,
    ) -> dict:
        root = root.expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError("SCAN-001 照片資料夾不存在或無法讀取")
        library_id = self.repository.ensure_library(name, root)
        checked = processed = skipped = new = changed = inherited = failed = 0
        last_progress_at = time.monotonic()
        with self.repository.signature_lookup(library_id) as signatures:
            for path in iter_images(root):
                if limit is not None and processed + failed >= limit:
                    break
                checked += 1
                try:
                    relative_path = path.relative_to(root).as_posix()
                    stat = path.stat()
                    stored = signatures.get(relative_path)
                    if stored and stored.matches(
                        file_size=stat.st_size, modified_time=stat.st_mtime
                    ):
                        if build_thumbnails and stored.sha256:
                            self.thumbnails.get_or_create(path, stored.sha256, 512)
                        skipped += 1
                    else:
                        state = "new" if stored is None else "changed"
                        features = self.preprocessor.analyze(path)
                        _, was_inherited = self.repository.upsert_preprocessed(
                            library_id, relative_path, path, features
                        )
                        if build_thumbnails:
                            self.thumbnails.get_or_create(path, features.sha256, 512)
                        inherited += int(was_inherited)
                        new += int(state == "new")
                        changed += int(state == "changed")
                        processed += 1
                except Exception:
                    failed += 1
                now = time.monotonic()
                if progress_callback and (
                    checked % max(1, progress_interval_items) == 0
                    or now - last_progress_at >= max(1, progress_interval_seconds)
                ):
                    progress_callback(
                        {
                            "checked": checked,
                            "processed": processed,
                            "skipped": skipped,
                            "new": new,
                            "changed": changed,
                            "inherited": inherited,
                            "failed": failed,
                        }
                    )
                    last_progress_at = now
        return {
            "library_id": library_id,
            "checked": checked,
            "processed": processed,
            "skipped": skipped,
            "new": new,
            "changed": changed,
            "inherited": inherited,
            "failed": failed,
        }
