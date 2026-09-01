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
        super().__init__(f"{self.code} 指定照片 {photo_id} 不符合正式發布資格：{reason}")


class RenderCandidateRepository:
    """正式 Release 的單一候選資格契約。

    SQL 部分由所有一般、歷史與排程流程共用；檔案系統部分在候選離開
    Repository 前再以 ``safe_join`` 驗證，避免 DB 仍為 active 時選到已移除
    或逃逸 Library Root 的檔案。
    """

    SQL_PREDICATE = """
        p.status='analyzed'
        AND p.eligible=1
        AND p.exclusion_status NOT IN ('auto_excluded','manually_excluded')
        AND p.lifecycle_status='active'
        AND l.enabled=1
        AND a.id IS NOT NULL AND a.schema_version=4
    """
    MAX_REQUESTED = 200

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def available(row: Any) -> bool:
        try:
            return safe_join(Path(str(row["root_path"])), str(row["relative_path"])).is_file()
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
                    WHERE latest.photo_id=p.id AND latest.schema_version=4
                    ORDER BY CASE WHEN latest.provider='local' THEN 1 ELSE 0 END,latest.created_at DESC,latest.id DESC LIMIT 1
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

    def get_local(self, photo_id: str) -> dict[str, Any] | None:
        """Local-only formal releases require scanner features, not AI rows."""
        with self.database.session() as connection:
            row = connection.execute(
                """
                SELECT p.*,l.root_path,l.enabled AS library_enabled
                FROM photos p JOIN libraries l ON l.id=p.library_id
                WHERE p.id=? AND p.lifecycle_status='active' AND p.eligible=1
                  AND p.local_features_status='complete' AND l.enabled=1
                  AND p.exclusion_status NOT IN ('auto_excluded','manually_excluded')
                """,
                (photo_id,),
            ).fetchone()
        return dict(row) if row is not None and self.available(row) else None

    def require_local(self, photo_ids: Iterable[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for photo_id in dict.fromkeys(str(value) for value in photo_ids if str(value)):
            row = self.get_local(photo_id)
            if row is None:
                raise IneligiblePhotoError(photo_id, "照片未完成本機特徵、已排除或原始檔不存在")
            rows.append(row)
        return rows

    def require_for_execution_mode(self, photo_ids: Iterable[str], execution: str) -> list[dict[str, Any]]:
        """Resolve one release contract per requested photo, without N+1 queries.

        Non-automatic modes deliberately accept a mix of old, fully analysed
        photos and scanner-only photos.  A contract is never chosen for the
        whole batch: doing so made a valid old photo fail merely because a
        second photo had only local features (and vice versa).
        """
        ids = list(dict.fromkeys(str(value).strip() for value in photo_ids if str(value).strip()))
        if len(ids) > self.MAX_REQUESTED:
            raise ValueError(f"RENDER-009 一次最多可發布 {self.MAX_REQUESTED} 張照片")
        if not ids:
            return []

        placeholders = ",".join("?" for _ in ids)
        with self.database.session() as connection:
            rows = connection.execute(
                f"""
                SELECT p.*, l.root_path, l.enabled AS library_enabled,
                       a.id AS latest_analysis_id
                FROM photos p
                LEFT JOIN libraries l ON l.id=p.library_id
                LEFT JOIN photo_analysis a ON a.id=(
                    SELECT latest.id FROM photo_analysis latest
                    WHERE latest.photo_id=p.id AND latest.schema_version=4
                    ORDER BY CASE WHEN latest.provider='local' THEN 1 ELSE 0 END,latest.created_at DESC, latest.id DESC LIMIT 1
                )
                WHERE p.id IN ({placeholders})
                """,  # noqa: S608 -- placeholders are generated only from the bounded input list.
                ids,
            ).fetchall()
        by_id = {str(row["id"]): dict(row) for row in rows}
        resolved: list[dict[str, Any]] = []
        for photo_id in ids:
            row = by_id.get(photo_id)
            if row is None:
                raise IneligiblePhotoError(photo_id, "PHOTO-ELIGIBILITY-001 照片不存在")
            if not bool(row.get("library_enabled")):
                raise IneligiblePhotoError(photo_id, "PHOTO-ELIGIBILITY-002 照片庫已停用")
            if str(row.get("lifecycle_status") or "") != "active":
                raise IneligiblePhotoError(
                    photo_id, "PHOTO-ELIGIBILITY-003 照片已 Missing 或不在 active 狀態"
                )
            if str(row.get("exclusion_status") or "eligible") in {"auto_excluded", "manually_excluded"}:
                raise IneligiblePhotoError(photo_id, "PHOTO-ELIGIBILITY-004 照片已排除")
            if not self.available(row):
                raise IneligiblePhotoError(photo_id, "PHOTO-ELIGIBILITY-005 原始檔不存在")

            analysis_eligible = (
                str(row.get("status") or "") == "analyzed"
                and bool(row.get("eligible"))
                and row.get("latest_analysis_id") is not None
            )
            local_eligible = (
                bool(row.get("eligible")) and str(row.get("local_features_status") or "") == "complete"
            )
            if execution == "automatic_ai":
                if not analysis_eligible:
                    raise IneligiblePhotoError(photo_id, "PHOTO-ELIGIBILITY-007 缺少有效正式分析")
                source = "analysis"
            elif analysis_eligible:
                source = "analysis"
            elif local_eligible:
                source = "local"
            else:
                raise IneligiblePhotoError(
                    photo_id, "PHOTO-ELIGIBILITY-006 本機特徵未完成，且缺少有效正式分析"
                )

            resolved.append(
                {
                    **row,
                    "eligibility_source": source,
                    "analysis_eligible": analysis_eligible,
                    "local_eligible": local_eligible,
                    "selected_contract": source,
                }
            )
        return resolved
