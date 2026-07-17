from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

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

    def scan(self, name: str, root: Path, *, build_thumbnails: bool = True, limit: int | None = None) -> dict:
        root = root.expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError("SCAN-001 照片資料夾不存在或無法讀取")
        library_id = self.repository.ensure_library(name, root)
        processed = inherited = failed = 0
        for path in iter_images(root):
            if limit is not None and processed + failed >= limit:
                break
            try:
                features = self.preprocessor.analyze(path)
                _, was_inherited = self.repository.upsert_preprocessed(
                    library_id, path.relative_to(root).as_posix(), path, features
                )
                if build_thumbnails:
                    self.thumbnails.get_or_create(path, features.sha256, 512)
                inherited += int(was_inherited)
                processed += 1
            except Exception:
                failed += 1
        return {"library_id": library_id, "processed": processed, "inherited": inherited, "failed": failed}
