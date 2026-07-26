from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from inktime.app.core.paths import safe_join
from inktime.app.repositories.photos import PhotoRepository


@dataclass(frozen=True)
class LegacyPhotoDTO:
    id: str
    relative_path: str
    library_name: str
    caption: str
    types: tuple[str, ...]
    memory_score: float | None
    beauty_score: float | None
    technical_quality_score: float | None
    emotion_score: float | None
    ranking_score: float | None
    side_caption: str
    reason: str
    captured_date: str | None
    captured_month_day: str | None
    width: int | None
    height: int | None


@dataclass(frozen=True)
class LegacyPhotoPage:
    items: tuple[LegacyPhotoDTO, ...]
    page: int
    page_size: int
    total: int


class LegacyPhotoRepositoryAdapter:
    """Read-only, bounded adapter backed exclusively by the modern repository."""

    MAX_PAGE_SIZE = 100
    MAX_OFFSET = 100_000

    def __init__(self, repository: PhotoRepository) -> None:
        self._repository = repository

    @staticmethod
    def _types(raw: object) -> tuple[str, ...]:
        try:
            value = json.loads(str(raw or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            value = []
        if not isinstance(value, list):
            return ()
        return tuple(str(item) for item in value if str(item).strip())

    def page(
        self,
        *,
        page: int = 1,
        page_size: int = 60,
        month_day: str = "",
        sort: str = "memory",
    ) -> LegacyPhotoPage:
        bounded_size = max(1, min(int(page_size), self.MAX_PAGE_SIZE))
        bounded_page = max(1, int(page))
        offset = min((bounded_page - 1) * bounded_size, self.MAX_OFFSET)
        rows, total = self._repository.compatibility_page(
            month_day=month_day,
            sort=sort,
            limit=bounded_size,
            offset=offset,
        )
        return LegacyPhotoPage(
            items=tuple(
                LegacyPhotoDTO(
                    id=str(row["id"]),
                    relative_path=str(row["relative_path"]),
                    library_name=str(row["library_name"]),
                    caption=str(row["caption"] or ""),
                    types=self._types(row["types_json"]),
                    memory_score=row["memory_score"],
                    beauty_score=row["beauty_score"],
                    technical_quality_score=row["technical_quality_score"],
                    emotion_score=row["emotion_score"],
                    ranking_score=row["ranking_score"],
                    side_caption=str(row["side_caption"] or ""),
                    reason=str(row["reason"] or ""),
                    captured_date=row["captured_date"],
                    captured_month_day=row["captured_month_day"],
                    width=row["width"],
                    height=row["height"],
                )
                for row in rows
            ),
            page=bounded_page,
            page_size=bounded_size,
            total=total,
        )

    def month_days(self) -> tuple[str, ...]:
        return tuple(self._repository.compatibility_month_days())

    def photo_path(self, photo_id: str) -> Path | None:
        row = self._repository.get_with_path(photo_id)
        if row is None:
            return None
        return safe_join(Path(str(row["root_path"])), str(row["relative_path"]))
