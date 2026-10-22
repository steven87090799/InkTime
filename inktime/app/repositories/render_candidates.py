from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from inktime.app.core.paths import UnsafePathError, safe_join
from inktime.app.db import Database
from inktime.app.domain.analysis.content_filter import evaluate_content_filter, evaluate_display_exclusion
from inktime.app.domain.analysis.schema import validate_analysis_result
from inktime.app.domain.analysis.scoring import (
    SEMANTIC_SCORE_KIND,
    preferred_analysis_order_sql,
)


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

    _BASE_SQL_PREDICATE = """
        p.status='analyzed'
        AND p.eligible=1
        AND p.never_display=0
        AND p.exclusion_status IN ('eligible','manually_restored')
        AND p.lifecycle_status='active'
        AND l.enabled=1
        AND a.id IS NOT NULL AND a.schema_version=6 AND a.score_kind='semantic'
    """
    SQL_PREDICATE = f"""
        {_BASE_SQL_PREDICATE}
        AND a.ranking_score IS NOT NULL
        AND p.local_features_status='complete'
    """
    SEMANTIC_SQL_PREDICATE = f"""
        {_BASE_SQL_PREDICATE}
        AND a.ranking_score IS NOT NULL
        AND p.local_features_status='complete'
    """
    LOCAL_SQL_PREDICATE = f"""
        {_BASE_SQL_PREDICATE}
        AND a.local_score IS NOT NULL
        AND p.local_features_status='complete'
    """
    MAX_REQUESTED = 200

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def available(row: Any) -> bool:
        try:
            return safe_join(Path(str(row["root_path"])), str(row["relative_path"]), decode_url=False).is_file()
        except (OSError, UnsafePathError, ValueError):
            return False

    @staticmethod
    def safety_passed(row: Any) -> bool:
        """Recheck stored model evidence even after an operator status change."""
        from inktime.app.repositories.photos import PhotoRepository

        try:
            analysis = validate_analysis_result(json.loads(row["raw_json"]))
            if analysis["schema_version"] != 6:
                return False
            decisions = (
                evaluate_content_filter(analysis["content_filter"], strict=True)["decision"],
                evaluate_display_exclusion(analysis["display_exclusion"])["decision"],
            )
            if "auto_excluded" in decisions:
                return False
            if "pending_review" in decisions and not (
                row["exclusion_status"] == "manually_restored" and row["manual_override"]
            ):
                return False
            return PhotoRepository._orientation_review(dict(row), analysis["visual_orientation"]) is None
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return False

    def get(self, photo_id: str) -> dict[str, Any] | None:
        with self.database.session() as connection:
            row = connection.execute(
                f"""
                SELECT p.*,l.root_path,l.enabled AS library_enabled,a.id AS latest_analysis_id,
                       a.score_kind,a.ranking_score,a.local_score,a.final_ranking_score,a.raw_json
                FROM photos p
                JOIN libraries l ON l.id=p.library_id
                LEFT JOIN photo_analysis a ON a.id=(
                    SELECT latest.id FROM photo_analysis latest
                    WHERE latest.photo_id=p.id
                    ORDER BY {preferred_analysis_order_sql('latest')} LIMIT 1
                )
                WHERE p.id=? AND {self.SQL_PREDICATE}
                """,  # noqa: S608 -- predicate is a fixed class constant
                (photo_id,),
            ).fetchone()
        if row is None or not self.available(row) or not self.safety_passed(row):
            return None
        return dict(row)

    def has_semantic_candidates(self) -> bool:
        """Return whether the current library has at least one AI-ranked row."""
        with self.database.session() as connection:
            row = connection.execute(
                f"""
                SELECT 1
                FROM photos p
                JOIN libraries l ON l.id=p.library_id
                JOIN photo_analysis a ON a.id=(
                    SELECT latest.id FROM photo_analysis latest
                    WHERE latest.photo_id=p.id
                    ORDER BY {preferred_analysis_order_sql('latest')} LIMIT 1
                )
                WHERE {self.SEMANTIC_SQL_PREDICATE}
                LIMIT 1
                """,  # noqa: S608 -- the predicate is a fixed repository constant.
            ).fetchone()
        return row is not None

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
        """A local ranking still needs v6 semantic safety evidence to publish."""
        return self.get(photo_id)

    def require_local(self, photo_ids: Iterable[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for photo_id in dict.fromkeys(str(value) for value in photo_ids if str(value)):
            row = self.get_local(photo_id)
            if row is None:
                raise IneligiblePhotoError(photo_id, "照片未完成本機特徵、已排除或原始檔不存在")
            rows.append(row)
        return rows

    def require_for_execution_mode(
        self,
        photo_ids: Iterable[str],
        execution: str,
    ) -> list[dict[str, Any]]:
        """Resolve one release contract per requested photo, without N+1 queries.

        Every formal release requires a current v6 semantic safety result and
        local scanner features. Older v4/v5 results remain readable but cannot
        certify the additional display-exclusion categories.
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
                       a.id AS latest_analysis_id,a.score_kind,a.ranking_score,a.schema_version,a.raw_json
                FROM photos p
                LEFT JOIN libraries l ON l.id=p.library_id
                LEFT JOIN photo_analysis a ON a.id=(
                    SELECT latest.id FROM photo_analysis latest
                    WHERE latest.photo_id=p.id
                    ORDER BY {preferred_analysis_order_sql('latest')} LIMIT 1
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
            if (bool(row.get("never_display"))
                    or str(row.get("exclusion_status") or "") not in {"eligible", "manually_restored"}):
                raise IneligiblePhotoError(photo_id, "PHOTO-ELIGIBILITY-004 照片已排除或等待人工判斷")
            if not self.available(row):
                raise IneligiblePhotoError(photo_id, "PHOTO-ELIGIBILITY-005 原始檔不存在")

            analysis_eligible = (
                str(row.get("status") or "") == "analyzed"
                and bool(row.get("eligible"))
                and row.get("latest_analysis_id") is not None
                and str(row.get("score_kind") or "") == SEMANTIC_SCORE_KIND
                and row.get("schema_version") == 6
                and row.get("ranking_score") is not None
            )
            local_eligible = (
                bool(row.get("eligible")) and str(row.get("local_features_status") or "") == "complete"
            )
            if not (analysis_eligible and local_eligible and self.safety_passed(row)):
                raise IneligiblePhotoError(
                    photo_id, "PHOTO-ELIGIBILITY-007 尚未同時完成本機品質檢查與 v6 AI 安全分析"
                )
            source = "analysis"

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
