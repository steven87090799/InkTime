from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from inktime.app.core.paths import UnsafePathError, safe_join
from inktime.app.db import Database


class IneligiblePhotoError(ValueError):
    code = "RENDER-009"

    def __init__(self, photo_id: str, reason: str) -> None:
        self.photo_id = photo_id
        self.reason = reason
        super().__init__(f"{self.code} 指定照片不符合正式發布資格：{reason}")


class RenderCandidateRepository:
    """正式 Release 的單一候選資格契約。

    SQL 部分由所有一般、歷史與排程流程共用；檔案系統部分在候選離開
    Repository 前再以 ``safe_join`` 驗證，避免 DB 仍為 active 時選到已移除
    或逃逸 Library Root 的檔案。
    """

    SQL_PREDICATE = """
        p.status='analyzed'
        AND p.eligible=1
        AND p.lifecycle_status='active'
        AND l.enabled=1
        AND a.id IS NOT NULL
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def available(row: Any) -> bool:
        try:
            return safe_join(
                Path(str(row["root_path"])), str(row["relative_path"])
            ).is_file()
        except (OSError, UnsafePathError, ValueError):
            return False

    def get(self, photo_id: str) -> dict[str, Any] | None:
        with self.database.session() as connection:
            row = connection.execute(
                f"""
                SELECT p.*,l.root_path,l.enabled AS library_enabled,a.id AS latest_analysis_id
                FROM photos p
                JOIN libraries l ON l.id=p.library_id
                LEFT JOIN photo_analysis a ON a.id=(
                    SELECT latest.id FROM photo_analysis latest
                    WHERE latest.photo_id=p.id
                    ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1
                )
                WHERE p.id=? AND {self.SQL_PREDICATE}
                """,  # noqa: S608 -- predicate is a fixed class constant
                (photo_id,),
            ).fetchone()
        if row is None or not self.available(row):
            return None
        return dict(row)

    def require(self, photo_ids: Iterable[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw_id in photo_ids:
            photo_id = str(raw_id).strip()
            if not photo_id or photo_id in seen:
                continue
            seen.add(photo_id)
            row = self.get(photo_id)
            if row is None:
                raise IneligiblePhotoError(
                    photo_id,
                    "照片可能已排除、Missing、刪除、缺少最新分析，或原始檔已不存在",
                )
            rows.append(row)
        return rows
