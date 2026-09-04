from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from threading import Lock
import time
from typing import Any, Iterable, Sequence
from uuid import uuid4

from inktime.app.core.paths import UnsafePathError, safe_join
from inktime.app.db import Database
from inktime.app.repositories.analysis_history import display_analysis_order_sql, historical_model_sql
from inktime.app.domain.analysis.scoring import (
    RANKING_RULE_VERSION,
    SEMANTIC_SCORE_KIND,
    preferred_analysis_order_sql,
    ranking_components,
    resolve_score_kind,
)
from inktime.app.domain.analysis.content_filter import CONTENT_FILTER_DEFAULTS, evaluate_content_filter
from inktime.app.domain.analysis.schema import REQUIRED_FIELDS, validate_analysis_result
from inktime.app.domain.photos.preprocessing import LocalPhotoFeatures
from inktime.app.domain.photos.quality_policy import (
    FEATURE_VERSION,
    QUALITY_POLICY_VERSION,
    evaluate_local_quality,
    is_confirmed_screenshot,
    local_candidate_score,
)
from inktime.app.domain.photos.dates import materialized_capture_fields, parse_photo_datetime


LOCAL_QUALITY_RULE = "local-quality"
LOCAL_QUALITY_RULE_VERSION = QUALITY_POLICY_VERSION
EXCLUDED_STATUSES = frozenset({"auto_excluded", "manually_excluded"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SCORE_POPULATION_TTL_SECONDS = 45.0
_SCORE_POPULATION_LOCK = Lock()
_SCORE_POPULATION_CACHE: tuple[str, float, tuple[float, ...]] | None = None


def invalidate_score_population_cache() -> None:
    """Invalidate the bounded process-local score distribution cache after writes."""

    global _SCORE_POPULATION_CACHE
    with _SCORE_POPULATION_LOCK:
        _SCORE_POPULATION_CACHE = None


def _effective_cache_version(prompt_version: str, vision_request_fingerprint: str | None) -> str:
    """Keep the legacy unique key while separating every Vision Input variant."""
    if not vision_request_fingerprint:
        return prompt_version
    return f"{prompt_version}@vision-{vision_request_fingerprint[:16]}"


def _stored_exclusion(photo: dict) -> tuple[str, dict] | None:
    """讓人工要求重新套用時使用同一規則與相同門檻。"""
    evaluation = evaluate_local_quality({**photo, "relative_path": str(photo.get("relative_path") or "")})
    if evaluation["decision"] != "auto_excluded":
        return None
    return str(evaluation["primary_reason"]), evaluation


def _must_preserve_exclusion(photo: dict | None, *, reapply_rules: bool = False) -> bool:
    """Keep exclusion state unless this write explicitly owns a permitted transition."""

    if not photo:
        return False
    status = str(photo.get("exclusion_status") or "eligible")
    if status == "manually_excluded":
        return True
    return status == "auto_excluded" and not reapply_rules


@dataclass(frozen=True)
class StoredPhotoSignature:
    id: str
    relative_path: str
    file_size: int | None
    modified_time: float | None
    sha256: str | None
    lifecycle_status: str
    metadata_status: str
    local_features_status: str
    feature_version: str
    status: str

    def matches(self, *, file_size: int, modified_time: float) -> bool:
        return bool(self.sha256) and self.file_size == file_size and self.modified_time == modified_time


@dataclass(frozen=True)
class PreparedScanPhoto:
    relative_path: str
    source: Path
    file_size: int
    modified_time: float
    features: LocalPhotoFeatures


@dataclass(frozen=True)
class BatchPhotoResult:
    relative_path: str
    photo_id: str
    action: str
    inherited: bool
    sha256: str


def _chunks(values: Sequence[str], size: int = 400) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


class PhotoRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ensure_library(self, name: str, root_path: Path) -> str:
        root = str(root_path.expanduser().resolve())
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            row = connection.execute("SELECT id FROM libraries WHERE root_path=?", (root,)).fetchone()
            if row:
                return str(row["id"])
            library_id = str(uuid4())
            connection.execute(
                "INSERT INTO libraries(id,name,root_path,created_at,updated_at) VALUES (?,?,?,?,?)",
                (library_id, name, root, now, now),
            )
            self._mark_library_ranking_dirty(connection, library_id, now)
            return library_id

    def signatures_for_paths(
        self, library_id: str, relative_paths: Sequence[str]
    ) -> dict[str, StoredPhotoSignature]:
        """每個磁碟批次只做固定數量 SQL，不逐張查詢。"""

        rows = []
        unique_paths = list(dict.fromkeys(relative_paths))
        with self.database.session() as connection:
            for chunk in _chunks(unique_paths):
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(
                    connection.execute(
                        f"""
                        SELECT id,relative_path,file_size,modified_time,sha256,lifecycle_status,
                               metadata_status,local_features_status,feature_version,status
                        FROM photos
                        WHERE library_id=? AND relative_path IN ({placeholders})
                        """,  # noqa: S608 -- placeholders are generated; values remain bound
                        (library_id, *chunk),
                    ).fetchall()
                )
        return {
            str(row["relative_path"]): StoredPhotoSignature(
                id=str(row["id"]),
                relative_path=str(row["relative_path"]),
                file_size=row["file_size"],
                modified_time=row["modified_time"],
                sha256=row["sha256"],
                lifecycle_status=str(row["lifecycle_status"]),
                metadata_status=str(row["metadata_status"]),
                local_features_status=str(row["local_features_status"]),
                feature_version=str(row["feature_version"] or ""),
                status=str(row["status"]),
            )
            for row in rows
        }

    def begin_scan(
        self,
        library_id: str,
        root: Path,
        *,
        mode: str,
        trigger_source: str,
        missing_threshold_ratio: float,
    ) -> str:
        scan_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            previous = int(
                connection.execute(
                    "SELECT COUNT(*) FROM photos WHERE library_id=? AND lifecycle_status='active'",
                    (library_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO scan_runs(
                    id,library_id,mode,trigger_source,status,root_path,root_accessible,
                    root_readable,previous_active_count,missing_threshold_ratio,started_at
                ) VALUES (?,?,?,?,'running',?,1,1,?,?,?)
                """,
                (
                    scan_id,
                    library_id,
                    mode,
                    trigger_source,
                    str(root),
                    previous,
                    min(1.0, max(0.0, float(missing_threshold_ratio))),
                    now,
                ),
            )
        return scan_id

    def mark_seen_batch(self, scan_id: str, photo_ids: Sequence[str]) -> None:
        if not photo_ids:
            return
        now = datetime.now(timezone.utc).isoformat()
        unique_ids = list(dict.fromkeys(photo_ids))
        with self.database.transaction() as connection:
            restored = 0
            for chunk in _chunks(unique_ids):
                placeholders = ",".join("?" for _ in chunk)
                cursor = connection.execute(
                    f"""
                    UPDATE photos SET lifecycle_status='active',missing_since=NULL,
                        missing_reason=NULL,updated_at=?
                    WHERE lifecycle_status='missing' AND id IN ({placeholders})
                    """,  # noqa: S608 -- placeholders are generated; values remain bound
                    (now, *chunk),
                )
                restored += int(cursor.rowcount)
                connection.execute(
                    f"UPDATE photos SET last_seen_scan_id=? WHERE id IN ({placeholders})",
                    (scan_id, *chunk),
                )
            scan = connection.execute(
                "SELECT library_id FROM scan_runs WHERE id=?", (scan_id,)
            ).fetchone()
            if scan is not None and restored:
                self._mark_library_ranking_dirty(connection, str(scan["library_id"]), now)

    def mark_processing_failed_batch(self, scan_id: str, failures: Sequence[tuple[str, bool, bool]]) -> None:
        """保留既有照片資料，但把本次未完成區段標成 failed 供增量重試。"""

        if not failures:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            photo_ids = list(dict.fromkeys(photo_id for photo_id, _metadata, _local in failures))
            restored = 0
            for chunk in _chunks(photo_ids):
                placeholders = ",".join("?" for _ in chunk)
                cursor = connection.execute(
                    f"""
                    UPDATE photos SET lifecycle_status='active',missing_since=NULL,
                        missing_reason=NULL,updated_at=?
                    WHERE lifecycle_status='missing' AND id IN ({placeholders})
                    """,  # noqa: S608 -- placeholders are generated; values remain bound
                    (now, *chunk),
                )
                restored += int(cursor.rowcount)
            connection.executemany(
                """
                UPDATE photos SET
                    last_seen_scan_id=?,
                    metadata_status=CASE WHEN ? THEN 'failed' ELSE metadata_status END,
                    local_features_status=CASE WHEN ? THEN 'failed' ELSE local_features_status END,
                    updated_at=?
                WHERE id=?
                """,
                [
                    (scan_id, int(metadata), int(local), now, photo_id)
                    for photo_id, metadata, local in failures
                ],
            )
            scan = connection.execute(
                "SELECT library_id FROM scan_runs WHERE id=?", (scan_id,)
            ).fetchone()
            if scan is not None and restored:
                self._mark_library_ranking_dirty(connection, str(scan["library_id"]), now)

    @staticmethod
    def _path_is_still_present(root: Path, relative_path: str) -> bool:
        try:
            return safe_join(root, relative_path).is_file()
        except UnsafePathError:
            # 不安全的舊資料不得被當成可自動搬移的來源。
            return True

    def apply_scan_batch(
        self,
        library_id: str,
        scan_id: str,
        root: Path,
        items: Sequence[PreparedScanPhoto],
        quality_policy_settings: dict | None = None,
    ) -> list[BatchPhotoResult]:
        """批次查詢、記憶體比對，再於單一交易寫入整批照片與初始狀態。"""

        if not items:
            return []
        now = datetime.now(timezone.utc).isoformat()
        paths = list(dict.fromkeys(item.relative_path for item in items))
        hashes = list(dict.fromkeys(item.features.sha256 for item in items))
        phashes = list(
            dict.fromkeys(
                item.features.perceptual_hash for item in items if item.features.perceptual_hash is not None
            )
        )
        # Resolve possible move sources before acquiring the writer
        # transaction.  A NAS/filesystem stat must never extend the SQLite
        # writer lock or make unrelated scans wait on remote I/O.
        candidate_paths: list[str] = []
        with self.database.session() as read_connection:
            for chunk in _chunks(hashes):
                placeholders = ",".join("?" for _ in chunk)
                candidate_paths.extend(
                    str(row["relative_path"])
                    for row in read_connection.execute(
                        f"SELECT relative_path FROM photos WHERE library_id=? AND sha256 IN ({placeholders})",  # noqa: S608
                        (library_id, *chunk),
                    ).fetchall()
                )
        present_candidate_paths = {
            relative_path: self._path_is_still_present(root, relative_path)
            for relative_path in dict.fromkeys(candidate_paths)
        }
        results: list[BatchPhotoResult] = []
        with self.database.transaction() as connection:
            path_rows: list[dict] = []
            for chunk in _chunks(paths):
                placeholders = ",".join("?" for _ in chunk)
                path_rows.extend(
                    dict(row)
                    for row in connection.execute(
                        f"SELECT * FROM photos WHERE library_id=? AND relative_path IN ({placeholders})",  # noqa: S608
                        (library_id, *chunk),
                    ).fetchall()
                )
            content_rows: list[dict] = []
            for chunk in _chunks(hashes):
                placeholders = ",".join("?" for _ in chunk)
                content_rows.extend(
                    dict(row)
                    for row in connection.execute(
                        f"""
                        SELECT p.* FROM photos p
                        WHERE p.library_id=? AND p.sha256 IN ({placeholders})
                          AND p.lifecycle_status IN ('active','missing')
                        """,  # noqa: S608
                        (library_id, *chunk),
                    ).fetchall()
                )
            phash_rows: list[dict] = []
            for chunk in _chunks(phashes):
                placeholders = ",".join("?" for _ in chunk)
                phash_rows.extend(
                    dict(row)
                    for row in connection.execute(
                        f"""
                        SELECT * FROM photos
                        WHERE library_id=? AND perceptual_hash IN ({placeholders})
                          AND lifecycle_status IN ('active','missing')
                        """,  # noqa: S608
                        (library_id, *chunk),
                    ).fetchall()
                )

            by_path = {str(row["relative_path"]): row for row in path_rows}
            by_hash: dict[str, list[dict]] = {}
            by_phash: dict[str, list[dict]] = {}
            for row in content_rows:
                by_hash.setdefault(str(row["sha256"]), []).append(row)
            for row in phash_rows:
                by_phash.setdefault(str(row["perceptual_hash"]), []).append(row)

            plans: list[dict] = []
            plans_by_id: dict[str, dict] = {}
            pending_by_hash: dict[str, list[dict]] = {}
            pending_by_phash: dict[str, list[dict]] = {}
            reserved_move_ids: set[str] = set()
            group_updates: dict[str, str] = {}
            changed_ids: list[str] = []
            old_groups: set[str] = set()
            move_parameters: list[tuple] = []

            def set_group(source: dict, group_id: str) -> None:
                source_id = str(source["id"])
                if source_id in plans_by_id:
                    plans_by_id[source_id]["duplicate_group_id"] = group_id
                elif not source.get("duplicate_group_id"):
                    group_updates[source_id] = group_id

            for item in items:
                features = item.features
                existing = by_path.get(item.relative_path)
                exact = [
                    row
                    for row in by_hash.get(features.sha256, [])
                    if existing is None or str(row["id"]) != str(existing["id"])
                ] + pending_by_hash.get(features.sha256, [])

                if existing is None:
                    movable = [
                        row
                        for row in exact
                        if str(row["id"]) not in reserved_move_ids
                        and str(row["id"]) not in plans_by_id
                        and not present_candidate_paths.get(str(row["relative_path"]), False)
                    ]
                    if len(movable) == 1:
                        source = movable[0]
                        photo_id = str(source["id"])
                        reserved_move_ids.add(photo_id)
                        move_parameters.append(
                            (
                                item.relative_path,
                                item.file_size,
                                item.modified_time,
                                scan_id,
                                now,
                                photo_id,
                            )
                        )
                        results.append(
                            BatchPhotoResult(
                                item.relative_path,
                                photo_id,
                                "moved",
                                False,
                                features.sha256,
                            )
                        )
                        continue

                content_changed = bool(existing and existing.get("sha256") != features.sha256)
                eligible_exact = [row for row in exact if str(row["id"]) not in reserved_move_ids]
                near = []
                if features.perceptual_hash is not None:
                    near = [
                        row
                        for row in by_phash.get(features.perceptual_hash, [])
                        + pending_by_phash.get(features.perceptual_hash, [])
                        if existing is None or str(row["id"]) != str(existing["id"])
                    ]
                duplicate_source = eligible_exact[0] if eligible_exact else (near[0] if near else None)
                inherited = bool(eligible_exact)
                if existing is not None:
                    photo_id = str(existing["id"])
                    action = "restored" if str(existing["lifecycle_status"]) == "missing" else "changed"
                    duplicate_group = existing.get("duplicate_group_id") if not content_changed else None
                    analysis_source = str(existing.get("analysis_source") or "direct")
                    status = str(existing["status"])
                    if content_changed:
                        status = "preprocessed" if features.local_features_complete else "discovered"
                        analysis_source = "inherited" if inherited else "direct"
                        changed_ids.append(photo_id)
                        if existing.get("duplicate_group_id"):
                            old_groups.add(str(existing["duplicate_group_id"]))
                    elif features.local_features_complete and status == "discovered":
                        status = "preprocessed"
                else:
                    photo_id = str(uuid4())
                    action = "new"
                    duplicate_group = None
                    analysis_source = "inherited" if inherited else "direct"
                    status = "preprocessed" if features.local_features_complete else "discovered"

                if duplicate_source is not None:
                    duplicate_group = (
                        duplicate_source.get("duplicate_group_id") or duplicate_group or str(uuid4())
                    )
                    set_group(duplicate_source, str(duplicate_group))

                plan = {
                    "kind": "update" if existing is not None else "insert",
                    "id": photo_id,
                    "item": item,
                    "existing": existing,
                    "status": status,
                    "analysis_source": analysis_source,
                    "duplicate_group_id": duplicate_group,
                    "content_changed": content_changed,
                }
                plans.append(plan)
                plans_by_id[photo_id] = plan
                pending = {
                    "id": photo_id,
                    "relative_path": item.relative_path,
                    "duplicate_group_id": duplicate_group,
                }
                pending_by_hash.setdefault(features.sha256, []).append(pending)
                if features.perceptual_hash is not None:
                    pending_by_phash.setdefault(features.perceptual_hash, []).append(pending)
                results.append(
                    BatchPhotoResult(
                        item.relative_path,
                        photo_id,
                        action,
                        inherited,
                        features.sha256,
                    )
                )

            # 前面出現的同批新照片可能在後面才被判定為 duplicate，回填其 group。
            for values in pending_by_hash.values():
                if len(values) < 2:
                    continue
                group_id = next(
                    (
                        str(plans_by_id[str(value["id"])]["duplicate_group_id"])
                        for value in values
                        if plans_by_id[str(value["id"])].get("duplicate_group_id")
                    ),
                    str(uuid4()),
                )
                for value in values:
                    plans_by_id[str(value["id"])]["duplicate_group_id"] = group_id

            if group_updates:
                connection.executemany(
                    "UPDATE photos SET duplicate_group_id=? WHERE id=?",
                    [(group_id, photo_id) for photo_id, group_id in group_updates.items()],
                )
            if move_parameters:
                connection.executemany(
                    """
                    UPDATE photos SET relative_path=?,file_size=?,modified_time=?,
                        lifecycle_status='active',missing_since=NULL,missing_reason=NULL,
                        last_seen_scan_id=?,updated_at=?
                    WHERE id=?
                    """,
                    move_parameters,
                )

            # 同一路徑換成不同內容時，舊照片的 Metadata／本地特徵已不再可信。
            # 先在同一交易清空並標為 pending，後續 UPDATE 再寫回本次實際完成的區段。
            if changed_ids:
                for chunk in _chunks(changed_ids):
                    placeholders = ",".join("?" for _ in chunk)
                    connection.execute(
                        f"""
                        UPDATE photos SET
                            exif_json=NULL,captured_at=NULL,captured_date=NULL,captured_month_day=NULL,
                            capture_date_status='pending',gps_lat=NULL,gps_lon=NULL,
                            exif_orientation_original=NULL,visual_orientation_rotation_cw=NULL,
                            visual_orientation_confidence=NULL,visual_orientation_ambiguous=1,
                            visual_orientation_evidence_json=NULL,manual_orientation_rotation_cw=NULL,
                            manual_orientation_updated_at=NULL,manual_orientation_updated_by=NULL,
                            metadata_status='pending',perceptual_hash=NULL,difference_hash=NULL,
                            brightness=NULL,contrast=NULL,blur_score=NULL,
                            overexposed_ratio=NULL,underexposed_ratio=NULL,
                            screenshot_likelihood=NULL,crop_focus_x=NULL,crop_focus_y=NULL,
                            crop_subject_left=NULL,crop_subject_top=NULL,crop_subject_right=NULL,
                            crop_subject_bottom=NULL,crop_method=NULL,crop_face_count=0,
                            crop_manual_x=NULL,crop_manual_y=NULL,local_features_status='pending',
                            e6_score=NULL,e6_contrast_score=NULL,e6_subject_score=NULL,
                            e6_skin_score=NULL,e6_text_score=NULL,e6_skin_pixels=0
                        WHERE id IN ({placeholders})
                        """,  # noqa: S608 -- placeholders are generated; IDs remain bound
                        chunk,
                    )
                    connection.execute(
                        f"DELETE FROM photo_analysis WHERE photo_id IN ({placeholders})",  # noqa: S608
                        chunk,
                    )

            update_parameters = []
            insert_parameters = []
            for plan in plans:
                item = plan["item"]
                features = item.features
                values = features.as_dict()
                if plan["kind"] == "update":
                    update_parameters.append(
                        (
                            item.file_size,
                            item.modified_time,
                            features.sha256,
                            features.width,
                            features.height,
                            features.format,
                            plan["status"],
                            plan["duplicate_group_id"],
                            plan["analysis_source"],
                            now,
                            scan_id,
                            int(features.metadata_complete),
                            values["exif_json"],
                            values["captured_at"],
                            values["gps_lat"],
                            values["gps_lon"],
                            int(features.local_features_complete),
                            values["perceptual_hash"],
                            values["difference_hash"],
                            values["brightness"],
                            values["contrast"],
                            values["blur_score"],
                            values["overexposed_ratio"],
                            values["underexposed_ratio"],
                            values["screenshot_likelihood"],
                            values["crop_focus_x"],
                            values["crop_focus_y"],
                            values["crop_subject_left"],
                            values["crop_subject_top"],
                            values["crop_subject_right"],
                            values["crop_subject_bottom"],
                            values["crop_method"],
                            values["crop_face_count"] or 0,
                            int(plan["content_changed"]),
                            plan["id"],
                        )
                    )
                else:
                    insert_parameters.append(
                        (
                            plan["id"],
                            library_id,
                            item.relative_path,
                            item.file_size,
                            item.modified_time,
                            features.sha256,
                            values["perceptual_hash"],
                            values["difference_hash"],
                            features.width,
                            features.height,
                            features.format,
                            plan["status"],
                            plan["duplicate_group_id"],
                            plan["analysis_source"],
                            now,
                            now,
                            values["exif_json"],
                            values["captured_at"],
                            values["gps_lat"],
                            values["gps_lon"],
                            values["brightness"],
                            values["contrast"],
                            values["blur_score"],
                            values["overexposed_ratio"],
                            values["underexposed_ratio"],
                            values["screenshot_likelihood"],
                            values["crop_focus_x"],
                            values["crop_focus_y"],
                            values["crop_subject_left"],
                            values["crop_subject_top"],
                            values["crop_subject_right"],
                            values["crop_subject_bottom"],
                            values["crop_method"],
                            values["crop_face_count"] or 0,
                            scan_id,
                            "complete" if features.metadata_complete else "pending",
                            "complete" if features.local_features_complete else "pending",
                        )
                    )

            if update_parameters:
                connection.executemany(
                    """
                    UPDATE photos SET
                        file_size=?,modified_time=?,sha256=?,width=?,height=?,format=?,status=?,
                        duplicate_group_id=?,analysis_source=?,updated_at=?,last_seen_scan_id=?,
                        lifecycle_status=CASE WHEN lifecycle_status='missing' THEN 'active' ELSE lifecycle_status END,
                        missing_since=CASE WHEN lifecycle_status='missing' THEN NULL ELSE missing_since END,
                        missing_reason=CASE WHEN lifecycle_status='missing' THEN NULL ELSE missing_reason END,
                        exif_json=CASE WHEN ? THEN ? ELSE exif_json END,
                        captured_at=CASE WHEN ? THEN ? ELSE captured_at END,
                        gps_lat=CASE WHEN ? THEN ? ELSE gps_lat END,
                        gps_lon=CASE WHEN ? THEN ? ELSE gps_lon END,
                        metadata_status=CASE WHEN ? THEN 'complete' ELSE metadata_status END,
                        perceptual_hash=CASE WHEN ? THEN ? ELSE perceptual_hash END,
                        difference_hash=CASE WHEN ? THEN ? ELSE difference_hash END,
                        brightness=CASE WHEN ? THEN ? ELSE brightness END,
                        contrast=CASE WHEN ? THEN ? ELSE contrast END,
                        blur_score=CASE WHEN ? THEN ? ELSE blur_score END,
                        overexposed_ratio=CASE WHEN ? THEN ? ELSE overexposed_ratio END,
                        underexposed_ratio=CASE WHEN ? THEN ? ELSE underexposed_ratio END,
                        screenshot_likelihood=CASE WHEN ? THEN ? ELSE screenshot_likelihood END,
                        crop_focus_x=CASE WHEN ? THEN ? ELSE crop_focus_x END,
                        crop_focus_y=CASE WHEN ? THEN ? ELSE crop_focus_y END,
                        crop_subject_left=CASE WHEN ? THEN ? ELSE crop_subject_left END,
                        crop_subject_top=CASE WHEN ? THEN ? ELSE crop_subject_top END,
                        crop_subject_right=CASE WHEN ? THEN ? ELSE crop_subject_right END,
                        crop_subject_bottom=CASE WHEN ? THEN ? ELSE crop_subject_bottom END,
                        crop_method=CASE WHEN ? THEN ? ELSE crop_method END,
                        crop_face_count=CASE WHEN ? THEN ? ELSE crop_face_count END,
                        local_features_status=CASE WHEN ? THEN 'complete' ELSE local_features_status END,
                        e6_score=CASE WHEN ? THEN NULL ELSE e6_score END,
                        e6_contrast_score=CASE WHEN ? THEN NULL ELSE e6_contrast_score END,
                        e6_subject_score=CASE WHEN ? THEN NULL ELSE e6_subject_score END,
                        e6_skin_score=CASE WHEN ? THEN NULL ELSE e6_skin_score END,
                        e6_text_score=CASE WHEN ? THEN NULL ELSE e6_text_score END,
                        e6_skin_pixels=CASE WHEN ? THEN 0 ELSE e6_skin_pixels END
                    WHERE id=?
                    """,
                    [
                        (
                            *params[:11],
                            params[11],
                            params[12],
                            params[11],
                            params[13],
                            params[11],
                            params[14],
                            params[11],
                            params[15],
                            params[11],
                            params[16],
                            params[17],
                            params[16],
                            params[18],
                            params[16],
                            params[19],
                            params[16],
                            params[20],
                            params[16],
                            params[21],
                            params[16],
                            params[22],
                            params[16],
                            params[23],
                            params[16],
                            params[24],
                            params[16],
                            params[25],
                            params[16],
                            params[26],
                            params[16],
                            params[27],
                            params[16],
                            params[28],
                            params[16],
                            params[29],
                            params[16],
                            params[30],
                            params[16],
                            params[31],
                            params[16],
                            params[32],
                            params[16],
                            params[33],
                            params[33],
                            params[33],
                            params[33],
                            params[33],
                            params[33],
                            params[34],
                        )
                        for params in update_parameters
                    ],
                )

            if insert_parameters:
                connection.executemany(
                    """
                    INSERT INTO photos(
                        id,library_id,relative_path,file_size,modified_time,sha256,
                        perceptual_hash,difference_hash,width,height,format,status,
                        duplicate_group_id,analysis_source,created_at,updated_at,exif_json,
                        captured_at,gps_lat,gps_lon,brightness,contrast,blur_score,
                        overexposed_ratio,underexposed_ratio,screenshot_likelihood,
                        crop_focus_x,crop_focus_y,crop_subject_left,crop_subject_top,
                        crop_subject_right,crop_subject_bottom,crop_method,crop_face_count,
                        lifecycle_status,last_seen_scan_id,metadata_status,local_features_status
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?)
                    """,
                    insert_parameters,
                )

            date_updates: list[tuple[str | None, str | None, str, str]] = []
            for plan in plans:
                features = plan["item"].features
                if not features.metadata_complete:
                    continue
                captured_date, month_day, status = materialized_capture_fields(
                    features.captured_at, warn=False
                )
                if features.capture_date_status == "invalid":
                    status = "invalid"
                date_updates.append((captured_date, month_day, status, plan["id"]))
            if date_updates:
                connection.executemany(
                    "UPDATE photos SET captured_date=?,captured_month_day=?,capture_date_status=? WHERE id=?",
                    date_updates,
                )

            # Local quality decisions preserve manual decisions across rescans.
            # Favorites retain the existing local-quality protection only;
            # AI content exclusion is independently enforced when saving analysis.
            quality_updates = []
            for plan in plans:
                features = plan["item"].features
                if not features.local_features_complete:
                    continue
                existing = plan["existing"]
                # Preserve content exclusions and explicit manual decisions.
                protected = _must_preserve_exclusion(existing) or (
                    bool(existing)
                    and (
                        bool(existing.get("favorite"))
                        or bool(existing.get("manual_override"))
                        or str(existing.get("exclusion_status") or "")
                        in {"manually_restored", "manually_excluded"}
                    )
                )
                policy = evaluate_local_quality(
                    {"relative_path": plan["item"].relative_path, **features.as_dict()},
                    settings=quality_policy_settings,
                )
                exclusion = (
                    None
                    if protected or policy["decision"] != "auto_excluded"
                    else (str(policy["primary_reason"]), policy)
                )
                if protected:
                    eligible = int(existing.get("eligible", 1))
                    exclusion_status = str(existing.get("exclusion_status") or "eligible")
                    reason = existing.get("reject_reason")
                    rule = existing.get("reject_rule")
                    rule_version = existing.get("reject_rule_version")
                    details = existing.get("reject_details_json")
                    rejected_at = existing.get("rejected_at")
                    manual_override = int(existing.get("manual_override") or 0)
                elif exclusion is None:
                    eligible = 1
                    exclusion_status = "eligible"
                    reason = rule = rule_version = details = rejected_at = None
                    manual_override = 0
                else:
                    reason, evidence = exclusion
                    eligible = 0
                    exclusion_status = "auto_excluded"
                    rule = LOCAL_QUALITY_RULE
                    rule_version = LOCAL_QUALITY_RULE_VERSION
                    details = json.dumps(
                        {
                            "reject_reason": reason,
                            "rule_version": rule_version,
                            **evidence,
                        },
                        ensure_ascii=False,
                    )
                    rejected_at = now
                    manual_override = 0
                quality_updates.append(
                    (
                        local_candidate_score(
                            features.as_dict(),
                            evaluation=policy,
                        ),
                        FEATURE_VERSION,
                        features.orientation,
                        int(plan["content_changed"]),
                        features.orientation,
                        features.orientation,
                        features.camera_make,
                        features.camera_model,
                        features.lens_model,
                        features.e6_score,
                        features.e6_contrast_score,
                        features.e6_subject_score,
                        features.e6_skin_score,
                        features.e6_text_score,
                        features.e6_skin_pixels,
                        eligible,
                        exclusion_status,
                        reason,
                        rule,
                        rule_version,
                        details,
                        rejected_at,
                        manual_override,
                        plan["id"],
                    )
                )
            if quality_updates:
                connection.executemany(
                    """
                    UPDATE photos SET local_candidate_score=?,feature_version=?,orientation=?,
                        exif_orientation_original=CASE WHEN ? THEN ? ELSE COALESCE(exif_orientation_original,?) END,
                        camera_make=?,camera_model=?,lens_model=?,e6_score=?,e6_contrast_score=?,
                        e6_subject_score=?,e6_skin_score=?,e6_text_score=?,e6_skin_pixels=?,eligible=?,exclusion_status=?,
                        reject_reason=?,reject_rule=?,reject_rule_version=?,reject_details_json=?,
                        rejected_at=?,manual_override=? WHERE id=?
                    """,
                    quality_updates,
                )

            for chunk in _chunks(sorted(old_groups)):
                placeholders = ",".join("?" for _ in chunk)
                connection.execute(
                    f"""
                    UPDATE photos SET duplicate_group_id=NULL
                    WHERE duplicate_group_id IN ({placeholders})
                      AND (SELECT COUNT(*) FROM photos other
                           WHERE other.duplicate_group_id=photos.duplicate_group_id) < 2
                    """,  # noqa: S608
                    chunk,
                )
            if changed_ids:
                self._mark_library_ranking_dirty(connection, library_id, now)
        if changed_ids:
            invalidate_score_population_cache()
        return results

    def record_scan_errors(self, scan_id: str, errors: Sequence[dict]) -> None:
        if not errors:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO scan_errors(
                    scan_id,photo_id,stage,error_code,exception_type,retryable,masked_path,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        scan_id,
                        error.get("photo_id"),
                        str(error["stage"])[:64],
                        str(error["error_code"])[:64],
                        str(error["exception_type"])[:128],
                        int(bool(error["retryable"])),
                        str(error["masked_path"])[:255],
                        now,
                    )
                    for error in errors
                ],
            )

    def finish_scan(
        self,
        scan_id: str,
        *,
        counts: dict[str, int],
        full_census: bool,
        cancelled: bool,
        major_io_errors: int,
    ) -> dict:
        """只在所有安全條件成立時，以單一 set-based 交易標記 Missing。"""

        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            scan = connection.execute("SELECT * FROM scan_runs WHERE id=?", (scan_id,)).fetchone()
            if scan is None:
                raise KeyError(scan_id)
            candidate_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM photos
                    WHERE library_id=? AND lifecycle_status='active'
                      AND COALESCE(last_seen_scan_id,'')<>?
                    """,
                    (scan["library_id"], scan_id),
                ).fetchone()[0]
            )
            baseline = int(scan["previous_active_count"])
            ratio = candidate_count / baseline if baseline else 0.0
            safe = bool(
                scan["root_accessible"]
                and scan["root_readable"]
                and full_census
                and not cancelled
                and major_io_errors == 0
            )
            threshold = float(scan["missing_threshold_ratio"])
            marked = 0
            warning_code = None
            reconciliation = "skipped"
            status = "completed"
            if cancelled:
                status = "cancelled"
                warning_code = "SCAN-CANCELLED"
            elif not safe:
                status = "completed_with_warnings"
                warning_code = "SCAN-IO-002" if major_io_errors else "SCAN-INCOMPLETE"
            elif ratio > threshold:
                status = "completed_with_warnings"
                warning_code = "SCAN-MISSING-THRESHOLD"
                reconciliation = "confirmation_required"
                connection.execute(
                    """
                    INSERT INTO scan_missing_candidates(scan_id,photo_id,created_at)
                    SELECT ?,id,? FROM photos
                    WHERE library_id=? AND lifecycle_status='active'
                      AND COALESCE(last_seen_scan_id,'')<>?
                    """,
                    (scan_id, now, scan["library_id"], scan_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE photos SET lifecycle_status='missing',missing_since=?,
                        missing_reason='not_seen_in_complete_scan',updated_at=?
                    WHERE library_id=? AND lifecycle_status='active'
                      AND COALESCE(last_seen_scan_id,'')<>?
                    """,
                    (now, now, scan["library_id"], scan_id),
                )
                marked = int(cursor.rowcount)
                reconciliation = "applied"
                if marked:
                    self._mark_library_ranking_dirty(connection, str(scan["library_id"]), now)
            connection.execute(
                """
                UPDATE scan_runs SET status=?,full_census=?,cancelled=?,major_io_errors=?,
                    checked_count=?,processed_count=?,skipped_count=?,new_count=?,changed_count=?,
                    moved_count=?,restored_count=?,duplicate_count=?,failed_count=?,excluded_video_count=?,
                    candidate_missing_count=?,missing_marked_count=?,reconciliation_status=?,
                    warning_code=?,completed_at=?
                WHERE id=?
                """,
                (
                    status,
                    int(full_census),
                    int(cancelled),
                    major_io_errors,
                    counts.get("checked", 0),
                    counts.get("processed", 0),
                    counts.get("skipped", 0),
                    counts.get("new", 0),
                    counts.get("changed", 0),
                    counts.get("moved", 0),
                    counts.get("restored", 0),
                    counts.get("duplicates", 0),
                    counts.get("failed", 0),
                    counts.get("excluded_videos", 0),
                    candidate_count,
                    marked,
                    reconciliation,
                    warning_code,
                    now,
                    scan_id,
                ),
            )
            result = dict(connection.execute("SELECT * FROM scan_runs WHERE id=?", (scan_id,)).fetchone())
        return result

    def confirm_missing(self, scan_id: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            scan = connection.execute("SELECT * FROM scan_runs WHERE id=?", (scan_id,)).fetchone()
            if scan is None:
                raise KeyError(scan_id)
            if scan["reconciliation_status"] != "confirmation_required":
                raise ValueError("SCAN-MISSING-002 此掃描不在等待 Missing 確認狀態")
            if not (
                scan["root_accessible"]
                and scan["root_readable"]
                and scan["full_census"]
                and not scan["cancelled"]
                and int(scan["major_io_errors"]) == 0
            ):
                raise ValueError("SCAN-MISSING-003 掃描安全條件不完整，禁止確認 Missing")
            newer = connection.execute(
                """
                SELECT 1 FROM scan_runs
                WHERE library_id=?
                  AND rowid > (SELECT rowid FROM scan_runs WHERE id=?)
                LIMIT 1
                """,
                (scan["library_id"], scan_id),
            ).fetchone()
            if newer:
                raise ValueError("SCAN-MISSING-004 已有較新的掃描，請只確認最新掃描結果")
            saved_candidates = int(
                connection.execute(
                    "SELECT COUNT(*) FROM scan_missing_candidates WHERE scan_id=?",
                    (scan_id,),
                ).fetchone()[0]
            )
            if saved_candidates != int(scan["candidate_missing_count"]):
                raise ValueError("SCAN-MISSING-003 Missing 候選結果不完整，禁止確認")
            cursor = connection.execute(
                """
                UPDATE photos SET lifecycle_status='missing',missing_since=?,
                    missing_reason='manually_confirmed_after_scan',updated_at=?
                WHERE lifecycle_status='active' AND id IN (
                    SELECT photo_id FROM scan_missing_candidates WHERE scan_id=?
                )
                """,
                (now, now, scan_id),
            )
            if cursor.rowcount:
                self._mark_library_ranking_dirty(connection, str(scan["library_id"]), now)
            connection.execute(
                """
                UPDATE scan_runs SET status='completed',reconciliation_status='confirmed',
                    missing_marked_count=?,warning_code=NULL
                WHERE id=?
                """,
                (int(cursor.rowcount), scan_id),
            )
            return int(cursor.rowcount)

    def get_scan(self, scan_id: str):
        with self.database.session() as connection:
            return connection.execute("SELECT * FROM scan_runs WHERE id=?", (scan_id,)).fetchone()

    def inherit_existing_analysis(
        self, photo_id: str, job_id: str | None, *, analysis_context: dict | None = None
    ) -> dict | None:
        required_fingerprint = str((analysis_context or {}).get("analysis_fingerprint") or "")
        with self.database.session() as connection:
            row = connection.execute(
                f"""
                SELECT a.*,source.local_candidate_score AS source_local_candidate_score,
                       source.visual_orientation_rotation_cw,source.visual_orientation_confidence,
                       source.visual_orientation_ambiguous,source.visual_orientation_evidence_json FROM photos target
                JOIN photos source ON source.sha256=target.sha256 AND source.id<>target.id
                JOIN photo_analysis a ON a.photo_id=source.id
                WHERE target.id=?
                  AND a.schema_version=4
                  AND (?='' OR a.analysis_fingerprint=?)
                ORDER BY {preferred_analysis_order_sql('a')} LIMIT 1
                """,
                (photo_id, required_fingerprint, required_fingerprint),
            ).fetchone()
        if row is None:
            return None
        if row["schema_version"] != 4:
            return None
        source_kind = resolve_score_kind(
            row["score_kind"] if "score_kind" in row.keys() else None,
            provider=row["provider"],
            stage=row["stage"],
        )
        semantic_available = source_kind == SEMANTIC_SCORE_KIND
        result = validate_analysis_result(row["raw_json"])
        self.save_analysis(
            photo_id,
            job_id,
            "inherited",
            row["provider"] or "inherited",
            row["model"] or "inherited",
            result,
            row["raw_json"],
            "inherited",
            ranking_score=row["ranking_score"],
            scoring_version_id=row["scoring_version_id"],
            score_kind=source_kind,
            local_score=result.get("local_score"),
            semantic_score=row["semantic_score"] if semantic_available else None,
            base_ranking_score=row["base_ranking_score"] if semantic_available else None,
            final_ranking_score=row["final_ranking_score"] if semantic_available else None,
            prompt_version=str(
                (analysis_context or {}).get("prompt_version") or row["prompt_version"] or "photo-quality-v3"
            ),
            analysis_fingerprint=(analysis_context or {}).get("analysis_fingerprint")
            or row["analysis_fingerprint"],
            analysis_spec_json=(analysis_context or {}).get("analysis_spec_json")
            or row["analysis_spec_json"],
            vision_request_fingerprint=(analysis_context or {}).get("vision_request_fingerprint")
            or row["vision_request_fingerprint"],
            vision_input_spec_json=(analysis_context or {}).get("vision_input_spec_json")
            or row["vision_input_spec_json"],
            inherited_from={
                "analysis_id": int(row["id"]),
                "photo_id": str(row["photo_id"]),
                "analysis_fingerprint": str(row["analysis_fingerprint"] or ""),
                "vision_request_fingerprint": str(row["vision_request_fingerprint"] or ""),
            },
        )
        return result

    def get_with_path(self, photo_id: str):
        with self.database.session() as connection:
            return connection.execute(
                f"""
                SELECT p.*, l.root_path,
                       (SELECT latest.score_kind FROM photo_analysis latest
                        WHERE latest.photo_id=p.id
                        ORDER BY {preferred_analysis_order_sql('latest')} LIMIT 1) AS latest_score_kind,
                       (SELECT latest.ranking_score FROM photo_analysis latest
                        WHERE latest.photo_id=p.id
                        ORDER BY {preferred_analysis_order_sql('latest')} LIMIT 1) AS latest_ranking_score
                FROM photos p JOIN libraries l ON l.id=p.library_id WHERE p.id=?
                """,
                (photo_id,),
            ).fetchone()

    def compatibility_page(
        self,
        *,
        month_day: str = "",
        sort: str = "memory",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list, int]:
        """Bounded read model for the temporary Legacy compatibility surface."""

        bounded_limit = max(1, min(int(limit), 100))
        bounded_offset = max(0, min(int(offset), 100_000))
        clauses = ["p.lifecycle_status='active'"]
        parameters: list[object] = []
        if month_day:
            parsed = parse_photo_datetime(f"2000-{month_day}", warn=False)
            if parsed is None:
                clauses.append("0")
            else:
                clauses.append("p.captured_month_day=?")
                parameters.append(month_day)
        orderings = {
            "memory": "COALESCE(a.memory_score,-1) DESC,COALESCE(a.visual_score,-1) DESC,p.id",
            "beauty": "COALESCE(a.visual_score,-1) DESC,COALESCE(a.memory_score,-1) DESC,p.id",
            "time_new": "(p.captured_date IS NULL),p.captured_date DESC,p.id",
            "time_old": "(p.captured_date IS NULL),p.captured_date ASC,p.id",
        }
        ordering_key = sort if sort in orderings else "memory"
        ordering = orderings[ordering_key]
        order_parameters: tuple[object, ...] = ()
        where = " AND ".join(clauses)
        latest_analysis = (
            "a.id=(SELECT latest.id FROM photo_analysis latest "
            "WHERE latest.photo_id=p.id ORDER BY "
            f"{preferred_analysis_order_sql('latest')} LIMIT 1)"
        )
        with self.database.session() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM photos p WHERE {where}",  # noqa: S608
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT p.id,p.relative_path,p.width,p.height,p.orientation,p.captured_date,
                       p.captured_month_day,p.gps_lat,p.gps_lon,p.exif_json,
                       l.name AS library_name,a.caption,a.types_json,a.memory_score,
                       a.visual_score,a.local_quality_score,
                       a.ranking_score,a.side_caption,a.reason,a.created_at AS analyzed_at
                FROM photos p JOIN libraries l ON l.id=p.library_id
                LEFT JOIN photo_analysis a ON {latest_analysis}
                WHERE {where} ORDER BY {ordering} LIMIT ? OFFSET ?
                """,  # noqa: S608 -- every fragment is selected from a fixed allowlist.
                (*parameters, *order_parameters, bounded_limit, bounded_offset),
            ).fetchall()
        return rows, total

    def compatibility_month_days(self) -> list[str]:
        """Return only indexed materialized dates; never parse full-library EXIF JSON."""

        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT DISTINCT captured_month_day FROM photos "
                "INDEXED BY idx_photos_captured_month_day "
                "WHERE lifecycle_status='active' AND captured_month_day IS NOT NULL "
                "ORDER BY captured_month_day LIMIT 367"
            ).fetchall()
        return [str(row[0]) for row in rows[:366]]

    def set_exclusion(
        self,
        photo_id: str,
        *,
        action: str,
        changed_by: str,
        reapply_rules: bool = False,
    ) -> dict:
        """以可稽核方式處理人工排除／恢復；只在明示時清除恢復覆寫。"""
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
                if row is None:
                    raise KeyError(photo_id)
                photo = dict(row)
                changes: dict[str, Any]
                if action == "restore":
                    values = (1, "manually_restored", 1, now, photo_id)
                    event = "manual_restore"
                    changes = {"manual_override": True, "eligible": True}
                    connection.execute(
                        "UPDATE photos SET eligible=?,exclusion_status=?,manual_override=?,updated_at=? WHERE id=?",
                        values,
                    )
                elif action == "exclude":
                    details = json.dumps(
                        {
                            "reject_reason": "manual_permanent_exclusion",
                            "rule_version": "manual-v1",
                            "measured_value": None,
                            "threshold": None,
                        },
                        ensure_ascii=False,
                    )
                    connection.execute(
                        """
                        UPDATE photos SET eligible=0,exclusion_status='manually_excluded',
                            reject_reason='manual_permanent_exclusion',reject_rule='manual',
                            reject_rule_version='manual-v1',reject_details_json=?,rejected_at=?,
                            manual_override=0,updated_at=? WHERE id=?
                        """,
                        (details, now, now, photo_id),
                    )
                    event = "manual_exclude"
                    changes = {"eligible": False, "reason": "manual_permanent_exclusion"}
                elif action == "favorite":
                    connection.execute(
                        "UPDATE photos SET favorite=1,updated_at=? WHERE id=?",
                        (now, photo_id),
                    )
                    event = "added_to_favorites"
                    changes = {"favorite": True}
                elif action == "candidate":
                    connection.execute(
                        """
                        UPDATE photos SET eligible=1,exclusion_status='pending_review',manual_override=1,
                            updated_at=? WHERE id=?
                        """,
                        (now, photo_id),
                    )
                    event = "added_to_candidate_pool"
                    changes = {"eligible": True, "candidate_pool": True}
                elif action == "reanalyze":
                    evaluation_photo = {**photo, "manual_override": 0, "favorite": 0, "exclusion_status": "eligible"} if reapply_rules else photo
                    exclusion = _stored_exclusion(evaluation_photo)
                    if reapply_rules and exclusion is None:
                        analysis = connection.execute(
                            "SELECT content_filter_json FROM photo_analysis "
                            "WHERE photo_id=? AND schema_version=4 AND score_kind=? "
                            "ORDER BY created_at DESC,id DESC LIMIT 1",
                            (photo_id, SEMANTIC_SCORE_KIND),
                        ).fetchone()
                        if analysis and analysis["content_filter_json"]:
                            content = evaluate_content_filter(json.loads(analysis["content_filter_json"]), self._content_settings(connection))
                            if content["decision"] == "auto_excluded":
                                exclusion = content["primary_reason"], content
                    protected = _must_preserve_exclusion(
                        photo, reapply_rules=reapply_rules
                    ) or ((bool(photo.get("manual_override")) or photo.get("exclusion_status") == "manually_restored") and not reapply_rules)
                    if protected:
                        connection.execute(
                            "UPDATE photos SET local_candidate_score=?,feature_version=?,updated_at=? WHERE id=?",
                            (
                                local_candidate_score(photo),
                                FEATURE_VERSION,
                                now,
                                photo_id,
                            ),
                        )
                    elif exclusion is None:
                        connection.execute(
                            """
                            UPDATE photos SET eligible=1,exclusion_status='eligible',reject_reason=NULL,
                                reject_rule=NULL,reject_rule_version=NULL,reject_details_json=NULL,
                                rejected_at=NULL,manual_override=0,feature_version=?,updated_at=? WHERE id=?
                            """,
                            (FEATURE_VERSION, now, photo_id),
                        )
                    else:
                        reason, evidence = exclusion
                        details = json.dumps(
                            {"reject_reason": reason, "rule_version": LOCAL_QUALITY_RULE_VERSION, **evidence},
                            ensure_ascii=False,
                        )
                        connection.execute(
                            """
                            UPDATE photos SET eligible=0,exclusion_status='auto_excluded',reject_reason=?,
                                reject_rule=?,reject_rule_version=?,reject_details_json=?,rejected_at=?,
                                manual_override=0,feature_version=?,updated_at=? WHERE id=?
                            """,
                            (
                                reason,
                                evidence.get("rule", LOCAL_QUALITY_RULE),
                                evidence.get("rule_version", LOCAL_QUALITY_RULE_VERSION),
                                details,
                                now,
                                FEATURE_VERSION,
                                now,
                                photo_id,
                            ),
                        )
                    event = "local_reanalysis"
                    changes = {
                        "reapply_rules": reapply_rules,
                        "manual_override": not reapply_rules and bool(photo.get("manual_override")),
                    }
                else:
                    raise ValueError("不支援的排除操作")
                connection.execute(
                    "INSERT INTO photo_events(photo_id,event,changes_json,changed_by,created_at) VALUES (?,?,?,?,?)",
                    (photo_id, event, json.dumps(changes, ensure_ascii=False), changed_by, now),
                )
                self._refresh_favorite_ranking(connection, photo_id)
                self._mark_library_ranking_dirty(connection, photo["library_id"], now)
                result = dict(connection.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone())
                connection.execute("COMMIT")
                invalidate_score_population_cache()
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def persist_prefilter_exclusion(self, photo_id: str, evaluation: dict) -> None:
        """Persist one automatic exclusion without disturbing manual state."""
        if evaluation.get("decision") != "auto_excluded":
            return
        now = datetime.now(timezone.utc).isoformat()
        details = json.dumps(
            {
                "policy_decision": evaluation["decision"],
                "primary_reason": evaluation["primary_reason"],
                "sensitivity": evaluation["sensitivity"],
                "matched_checks": evaluation["matched_checks"],
                "thresholds": evaluation["thresholds"],
                "e6_threshold": evaluation.get("e6_threshold"),
                "feature_version": evaluation["feature_version"],
                "policy_version": evaluation["policy_version"],
                "evidence": evaluation["evidence"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT library_id FROM photos WHERE id=?", (photo_id,)
            ).fetchone()
            cursor = connection.execute(
                """UPDATE photos SET eligible=0,exclusion_status='auto_excluded',reject_reason=?,
                   reject_rule=?,reject_rule_version=?,reject_details_json=?,rejected_at=?,updated_at=?
                   WHERE id=? AND favorite=0 AND manual_override=0
                   AND exclusion_status NOT IN ('manually_restored','manually_excluded')""",
                (
                    evaluation["primary_reason"],
                    LOCAL_QUALITY_RULE,
                    QUALITY_POLICY_VERSION,
                    details,
                    now,
                    now,
                    photo_id,
                ),
            )
            if cursor.rowcount and row is not None:
                self._mark_library_ranking_dirty(connection, str(row["library_id"]), now)
        invalidate_score_population_cache()

    def record_force_ai_event(
        self, photo_id: str, *, job_id: str | None, provider: str, provider_name: str, model: str, actor: str
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            # Background jobs commonly use the descriptive actor "system";
            # it is not an authenticated users.id and must not violate the
            # photo_events.changed_by foreign key.
            actor_id = connection.execute(
                "SELECT id FROM users WHERE id=?", (actor,)
            ).fetchone()
            connection.execute(
                "INSERT INTO photo_events(photo_id,event,changes_json,changed_by,created_at) VALUES (?,?,?,?,?)",
                (
                    photo_id,
                    "force_ai_analysis_completed",
                    json.dumps(
                        {
                            "job_id": job_id,
                            "provider_id": provider,
                            "provider_name": provider_name,
                            "model": model,
                        },
                        ensure_ascii=False,
                    ),
                    actor if actor_id is not None else None,
                    now,
                ),
            )

    def record_analysis_request_outcome(
        self,
        *,
        photo_id: str,
        job_id: str | None,
        provider: str,
        model: str,
        request_fingerprint: str,
        outcome: str,
        error_code: str | None = None,
        error_message: str | None = None,
        requires_manual_confirmation: bool = False,
    ) -> None:
        """Persist an AI request outcome without treating an unknown POST as safe to retry."""
        if outcome not in {"completed", "ambiguous_failed", "failed"}:
            raise ValueError("ANALYSIS-OUTCOME-001 outcome 不合法")
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO analysis_request_outcomes(
                    photo_id,job_id,provider,model,request_fingerprint,outcome,
                    error_code,error_message,requires_manual_confirmation,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    photo_id,
                    job_id,
                    provider[:128],
                    model[:128],
                    request_fingerprint[:128],
                    outcome,
                    (str(error_code)[:64] if error_code else None),
                    (str(error_message)[:500] if error_message else None),
                    int(bool(requires_manual_confirmation)),
                    now,
                ),
            )

    def search_exclusions(
        self,
        *,
        reason: str = "",
        year: str = "",
        folder: str = "",
        kind: str = "",
        origin: str = "",
        limit: int = 200,
    ) -> list:
        # ``manually_restored`` is an audit state, not an exclusion.  Use the
        # effective eligibility plus review states so a successful restore
        # actually leaves this management page.
        clauses = [
            "(p.eligible=0 OR p.exclusion_status IN ('auto_excluded','manually_excluded','pending_review'))"
        ]
        parameters: list = []
        if reason:
            clauses.append("p.reject_reason=?")
            parameters.append(reason)
        if year and year.isdigit() and len(year) == 4:
            clauses.append("substr(COALESCE(p.captured_at,p.created_at),1,4)=?")
            parameters.append(year)
        if folder:
            clauses.append("p.relative_path LIKE ? ESCAPE '\\'")
            escaped = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.append(escaped + "%")
        if kind == "screenshot":
            clauses.append("p.reject_reason='screenshot'")
        elif kind == "document":
            clauses.append("p.reject_reason='document_or_receipt'")
        elif kind == "duplicate":
            clauses.append("p.duplicate_group_id IS NOT NULL")
        if origin == "manual":
            clauses.append("p.exclusion_status='manually_excluded'")
        elif origin == "auto":
            clauses.append("p.exclusion_status='auto_excluded'")
        where = " AND ".join(clauses)
        with self.database.session() as connection:
            return connection.execute(
                f"""
                SELECT p.*,l.name AS library_name,a.provider,a.model,a.created_at AS analyzed_at
                FROM photos p JOIN libraries l ON l.id=p.library_id
                LEFT JOIN photo_analysis a ON a.id=(
                    SELECT id FROM photo_analysis WHERE photo_id=p.id
                    ORDER BY {preferred_analysis_order_sql()} LIMIT 1
                )
                WHERE {where}
                ORDER BY p.rejected_at DESC,p.updated_at DESC,p.id DESC LIMIT ?
                """,
                (*parameters, max(1, min(int(limit), 500))),
            ).fetchall()

    def eligible_photo_ids(self, *, limit: int | None = None, include_all_active: bool = False) -> list[str]:
        where = "p.lifecycle_status='active' AND p.local_features_status='complete'"
        if not include_all_active:
            where += " AND p.eligible=1"
        # Advance through pending photos on successive bounded runs. Technical
        # quality is a gate, not a permanent top-N admission score for the AI.
        query = f"""SELECT p.id FROM photos p WHERE {where}
            ORDER BY EXISTS(
                SELECT 1 FROM photo_analysis a WHERE a.photo_id=p.id
                AND a.schema_version=4 AND a.score_kind='semantic'
                AND a.ranking_score IS NOT NULL
            ),p.captured_at DESC,p.id"""  # noqa: S608 -- fixed internal predicate
        params: tuple = () if limit is None else (max(1, min(int(limit), 100_000)),)
        if limit is not None:
            query += " LIMIT ?"
        with self.database.session() as connection:
            return [str(row["id"]) for row in connection.execute(query, params).fetchall()]

    def count_active_eligible(self) -> int:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM photos WHERE lifecycle_status='active' AND eligible=1"
            ).fetchone()
        return int(row[0] or 0)

    def confirmed_screenshot_ids(self, photo_ids: Sequence[str]) -> set[str]:
        """Return explicit screenshot ids without reading image bytes."""
        requested = list(dict.fromkeys(str(photo_id) for photo_id in photo_ids))
        blocked: set[str] = set()
        with self.database.session() as connection:
            for chunk in _chunks(requested, 400):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT id,relative_path,exif_json,screenshot_likelihood FROM photos WHERE id IN ({placeholders})",  # noqa: S608
                    tuple(chunk),
                ).fetchall()
                blocked.update(str(row["id"]) for row in rows if is_confirmed_screenshot(row))
        return blocked

    def active_hashes_for(self, cache_hashes: Sequence[str]) -> set[str]:
        """Look up only cache-visible SHA values; never materialize the photo library."""
        requested = list(
            dict.fromkeys(
                value.casefold() for value in cache_hashes if _SHA256_RE.fullmatch(value.casefold())
            )
        )
        active: set[str] = set()
        with self.database.session() as connection:
            for chunk in _chunks(requested, 400):
                placeholders = ",".join("?" for _ in chunk)
                active.update(
                    str(row["sha256"]).casefold()
                    for row in connection.execute(
                        f"SELECT DISTINCT sha256 FROM photos WHERE lifecycle_status='active' AND sha256 IN ({placeholders})",  # noqa: S608
                        tuple(chunk),
                    ).fetchall()
                )
        return active

    def active_eligible_requested_ids(self, photo_ids: Sequence[str], *, limit: int) -> list[str]:
        """Bounded SQL validation that preserves the caller's ordering."""
        requested = list(dict.fromkeys(str(photo_id) for photo_id in photo_ids))
        allowed: set[str] = set()
        with self.database.session() as connection:
            for chunk in _chunks(requested, 400):
                placeholders = ",".join("?" for _ in chunk)
                allowed.update(
                    str(row["id"])
                    for row in connection.execute(
                        f"SELECT id FROM photos WHERE lifecycle_status='active' AND eligible=1 AND id IN ({placeholders})",  # noqa: S608
                        tuple(chunk),
                    ).fetchall()
                )
        return [photo_id for photo_id in requested if photo_id in allowed][: max(1, limit)]

    def eligible_photo_batches(
        self, *, group_by: str, limit: int, include_all_active: bool = False
    ) -> list[tuple[str, list[str]]]:
        """完整照片庫模式以年份或第一層資料夾拆成可暫停／續跑的既有工作。"""
        where = (
            "lifecycle_status='active'" if include_all_active else "lifecycle_status='active' AND eligible=1"
        )
        remaining = max(1, min(int(limit), 100_000))
        with self.database.session() as connection:
            rows = connection.execute(
                f"""
                SELECT id,relative_path,captured_at,created_at FROM photos WHERE {where}
                ORDER BY COALESCE(captured_at,created_at),relative_path,id
                LIMIT ?
                """,
                (remaining,),
            ).fetchall()
        groups: dict[str, list[str]] = {}
        for row in rows:
            path = str(row["relative_path"] or "")
            key = (
                path.split("/", 1)[0] or "根目錄"
                if group_by == "folder"
                else str(row["captured_at"] or row["created_at"] or "未知")[:4]
            )
            groups.setdefault(key or "未知", []).append(str(row["id"]))
        return list(groups.items())

    def is_top_candidate(self, photo_id: str, limit: int) -> bool:
        return photo_id in set(self.eligible_photo_ids(limit=max(1, min(int(limit), 10_000))))

    def ai_limit_reached(self, *, daily_limit: int, monthly_limit: int) -> bool:
        with self.database.session() as connection:
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT CASE WHEN date(started_at)=date('now') THEN photo_id END) AS daily,
                       COUNT(DISTINCT CASE WHEN strftime('%Y-%m',started_at)=strftime('%Y-%m','now') THEN photo_id END) AS monthly
                FROM api_usage WHERE provider != 'local' AND photo_id IS NOT NULL
                """
            ).fetchone()
        return int(row["daily"] or 0) >= daily_limit or int(row["monthly"] or 0) >= monthly_limit

    def location_visit_count(self, latitude: float | None, longitude: float | None) -> int:
        """以約 22 km 的本機格網估計地點稀有度，避免把精確座標送出。"""
        if latitude is None or longitude is None:
            return 0
        delta = 0.2
        with self.database.session() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM photos WHERE gps_lat BETWEEN ? AND ? AND gps_lon BETWEEN ? AND ?
                    """,
                    (
                        float(latitude) - delta,
                        float(latitude) + delta,
                        float(longitude) - delta,
                        float(longitude) + delta,
                    ),
                ).fetchone()[0]
            )

    def get_ai_cache(
        self,
        *,
        content_sha256: str,
        provider: str,
        model_name: str,
        prompt_version: str,
        schema_version: int,
        schema_kind: str,
        vision_request_fingerprint: str | None = None,
    ) -> dict | None:
        cache_prompt_version = _effective_cache_version(prompt_version, vision_request_fingerprint)
        with self.database.session() as connection:
            row = connection.execute(
                """
                SELECT * FROM ai_analysis_cache WHERE content_sha256=? AND provider=? AND model_name=?
                  AND prompt_version=? AND schema_version=? AND schema_kind=?
                  AND (? IS NULL OR vision_request_fingerprint=?)
                """,
                (
                    content_sha256,
                    provider,
                    model_name,
                    cache_prompt_version,
                    schema_version,
                    schema_kind,
                    vision_request_fingerprint,
                    vision_request_fingerprint,
                ),
            ).fetchone()
        if row is None:
            return None
        cached = dict(row)
        try:
            cached["result"] = json.loads(str(cached["result_json"]))
        except json.JSONDecodeError:
            return None
        return cached

    def put_ai_cache(
        self,
        *,
        content_sha256: str,
        provider: str,
        model_name: str,
        prompt_version: str,
        schema_version: int,
        schema_kind: str,
        result: dict,
        raw_json: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        estimated_cost: float,
        latency_ms: int,
        vision_request_fingerprint: str | None = None,
        vision_input_spec_json: str | None = None,
        connection=None,
    ) -> None:
        cache_prompt_version = _effective_cache_version(prompt_version, vision_request_fingerprint)
        context = self.database.session() if connection is None else nullcontext(connection)
        with context as active_connection:
            connection = active_connection
            connection.execute(
                """
                INSERT INTO ai_analysis_cache(content_sha256,provider,model_name,prompt_version,schema_version,schema_kind,
                    result_json,raw_json,input_tokens,output_tokens,cached_tokens,estimated_cost,latency_ms,created_at,
                    vision_request_fingerprint,vision_input_spec_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(content_sha256,provider,model_name,prompt_version,schema_version,schema_kind)
                DO UPDATE SET result_json=excluded.result_json,raw_json=excluded.raw_json,input_tokens=excluded.input_tokens,
                    output_tokens=excluded.output_tokens,cached_tokens=excluded.cached_tokens,estimated_cost=excluded.estimated_cost,
                    latency_ms=excluded.latency_ms,created_at=excluded.created_at,
                    vision_request_fingerprint=excluded.vision_request_fingerprint,
                    vision_input_spec_json=excluded.vision_input_spec_json
                """,
                (
                    content_sha256,
                    provider,
                    model_name,
                    cache_prompt_version,
                    schema_version,
                    schema_kind,
                    json.dumps(result, ensure_ascii=False),
                    raw_json,
                    input_tokens,
                    output_tokens,
                    cached_tokens,
                    estimated_cost,
                    latency_ms,
                    datetime.now(timezone.utc).isoformat(),
                    vision_request_fingerprint,
                    vision_input_spec_json,
                ),
            )

    def acquire_ai_cache_reservation(
        self, cache_key: str, owner_id: str, *, lease_seconds: int = 480
    ) -> bool:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_until = (now_dt + timedelta(seconds=max(5, lease_seconds))).isoformat()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT owner_id,status,lease_until FROM ai_cache_reservations WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO ai_cache_reservations(
                        cache_key,owner_id,status,lease_until,created_at,updated_at
                    ) VALUES (?,?,'reserved',?,?,?)
                    """,
                    (cache_key, owner_id, lease_until, now, now),
                )
                return True
            # A completed marker without a matching cache row is harmless: the
            # caller always rechecks cache after acquiring, then may take over.
            takeover = str(row["status"]) in {"failed", "completed"} or str(row["lease_until"]) <= now
            if not takeover:
                return str(row["owner_id"]) == owner_id
            connection.execute(
                """
                UPDATE ai_cache_reservations
                SET owner_id=?,status='reserved',lease_until=?,updated_at=?,last_error=NULL
                WHERE cache_key=?
                """,
                (owner_id, lease_until, now, cache_key),
            )
            return True

    def finish_ai_cache_reservation(self, cache_key: str, owner_id: str, *, error: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute(
                """
                UPDATE ai_cache_reservations
                SET status=?,updated_at=?,last_error=?
                WHERE cache_key=? AND owner_id=? AND status='reserved'
                """,
                (
                    "failed" if error else "completed",
                    now,
                    error[:500] if error else None,
                    cache_key,
                    owner_id,
                ),
            )

    def list_existing_photo_ids(self, library_id: str, root: Path, *, limit: int) -> list[str]:
        """依檔案修改時間挑選仍存在於指定照片庫內的照片。"""
        bounded_limit = max(1, min(int(limit), 100))
        root = root.expanduser().resolve()
        selected: list[str] = []
        offset = 0
        while len(selected) < bounded_limit:
            with self.database.session() as connection:
                rows = connection.execute(
                    """
                    SELECT id,relative_path FROM photos
                    WHERE library_id=?
                    ORDER BY modified_time DESC,id DESC LIMIT 100 OFFSET ?
                    """,
                    (library_id, offset),
                ).fetchall()
            if not rows:
                break
            for row in rows:
                try:
                    path = safe_join(root, str(row["relative_path"]))
                except UnsafePathError:
                    continue
                if path.is_file():
                    selected.append(str(row["id"]))
                    if len(selected) >= bounded_limit:
                        break
            offset += len(rows)
        return selected

    def update_manual(
        self,
        photo_id: str,
        *,
        favorite: bool,
        captured_at: str | None,
        types: list[str],
        side_caption: str,
        changed_by: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        normalized_captured_at = None
        if captured_at:
            parsed = parse_photo_datetime(captured_at)
            if parsed is None:
                raise ValueError("IMG-004 拍攝日期格式不合法")
            normalized_captured_at = parsed.isoformat()
        captured_date, month_day, date_status = materialized_capture_fields(
            normalized_captured_at, warn=False
        )
        changes = {
            "favorite": favorite,
            "captured_at": normalized_captured_at,
            "types": types,
            "side_caption": side_caption,
        }
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE photos SET favorite=?,captured_at=?,captured_date=?,"
                    "captured_month_day=?,capture_date_status=?,updated_at=? WHERE id=?",
                    (
                        int(favorite),
                        normalized_captured_at,
                        captured_date,
                        month_day,
                        date_status,
                        now,
                        photo_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise KeyError(photo_id)
                latest = connection.execute(
                    f"SELECT id FROM photo_analysis WHERE photo_id=? ORDER BY {preferred_analysis_order_sql()} LIMIT 1",
                    (photo_id,),
                ).fetchone()
                if latest:
                    connection.execute(
                        "UPDATE photo_analysis SET types_json=?,side_caption=? WHERE id=?",
                        (json.dumps(types, ensure_ascii=False), side_caption, latest["id"]),
                    )
                    self._refresh_favorite_ranking(connection, photo_id)
                library = connection.execute(
                    "SELECT library_id FROM photos WHERE id=?", (photo_id,)
                ).fetchone()
                self._mark_library_ranking_dirty(connection, str(library["library_id"]), now)
                connection.execute(
                    "INSERT INTO photo_events(photo_id,event,changes_json,changed_by,created_at) VALUES (?,'manual_update',?,?,?)",
                    (photo_id, json.dumps(changes, ensure_ascii=False), changed_by, now),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        invalidate_score_population_cache()

    def set_upload_privacy(self, photo_id: str, *, never_upload: bool, changed_by: str) -> dict:
        """Toggle model-upload privacy without changing display or saved analysis."""

        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT id,never_upload,never_display FROM photos WHERE id=?", (photo_id,)
            ).fetchone()
            if row is None:
                raise KeyError(photo_id)
            connection.execute(
                "UPDATE photos SET never_upload=?,updated_at=? WHERE id=?",
                (int(never_upload), now, photo_id),
            )
            connection.execute(
                "INSERT INTO photo_events(photo_id,event,changes_json,changed_by,created_at) VALUES (?,?,?,?,?)",
                (
                    photo_id,
                    "upload_privacy_changed",
                    json.dumps({"never_upload": bool(never_upload)}, ensure_ascii=False),
                    changed_by,
                    now,
                ),
            )
        return {
            "id": photo_id,
            "never_upload": bool(never_upload),
            "never_display": bool(row["never_display"]),
        }

    def update_crop(self, photo_id: str, *, manual_x: float | None, manual_y: float | None) -> None:
        if (manual_x is None) != (manual_y is None):
            raise ValueError("裁切 X 與 Y 必須同時設定或同時清除")
        if (
            manual_x is not None
            and manual_y is not None
            and not (0.0 <= manual_x <= 1.0 and 0.0 <= manual_y <= 1.0)
        ):
            raise ValueError("裁切位置必須介於 0 到 1")
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            cursor = connection.execute(
                "UPDATE photos SET crop_manual_x=?,crop_manual_y=?,updated_at=? WHERE id=?",
                (manual_x, manual_y, now, photo_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(photo_id)

    def update_crop_analysis(self, photo_id: str, analysis) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            cursor = connection.execute(
                """
                UPDATE photos SET crop_focus_x=?,crop_focus_y=?,crop_subject_left=?,
                    crop_subject_top=?,crop_subject_right=?,crop_subject_bottom=?,
                    crop_method=?,crop_face_count=?,updated_at=?
                WHERE id=?
                """,
                (
                    analysis.focus_x,
                    analysis.focus_y,
                    analysis.subject_left,
                    analysis.subject_top,
                    analysis.subject_right,
                    analysis.subject_bottom,
                    analysis.method,
                    analysis.face_count,
                    now,
                    photo_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(photo_id)

    def update_e6_suitability(self, photo_id: str, metrics) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            cursor = connection.execute(
                """
                UPDATE photos SET e6_score=?,e6_contrast_score=?,e6_subject_score=?,
                    e6_skin_score=?,e6_text_score=?,e6_skin_pixels=?,updated_at=?
                WHERE id=?
                """,
                (
                    metrics.score,
                    metrics.contrast_score,
                    metrics.subject_score,
                    metrics.skin_score,
                    metrics.text_score,
                    metrics.skin_pixels,
                    now,
                    photo_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(photo_id)

    def search(
        self,
        *,
        query: str = "",
        status: str = "",
        photo_type: str = "",
        minimum_score: float | None = None,
        duplicate_only: bool = False,
        limit: int = 200,
        offset: int = 0,
    ):
        clauses = ["1=1"]
        parameters: list = []
        if query:
            clauses.append("(p.relative_path LIKE ? ESCAPE '\\' OR a.caption LIKE ? ESCAPE '\\')")
            escaped = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            parameters.extend([escaped, escaped])
        if status:
            clauses.append("p.status=?")
            parameters.append(status)
        if photo_type:
            clauses.append("a.types_json LIKE ?")
            parameters.append(f'%"{photo_type}"%')
        if minimum_score is not None:
            clauses.append("a.schema_version=4 AND a.score_kind=? AND a.memory_score>=?")
            parameters.append(SEMANTIC_SCORE_KIND)
            parameters.append(minimum_score)
        if duplicate_only:
            clauses.append("p.duplicate_group_id IS NOT NULL")
        where = " AND ".join(clauses)
        with self.database.session() as connection:
            total = int(
                connection.execute(
                    f"""
                SELECT COUNT(*) FROM photos p
                LEFT JOIN photo_analysis a ON a.id=(SELECT display.id FROM photo_analysis display WHERE display.photo_id=p.id ORDER BY {display_analysis_order_sql('display')} LIMIT 1)
                WHERE {where}
                """,
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT p.*,l.name AS library_name,a.caption,a.types_json,a.memory_score,
                       a.visual_score,a.local_quality_score,a.ranking_score,a.side_caption,
                       a.local_score,a.score_kind,a.provider,a.model,a.raw_json,a.schema_version,
                       {historical_model_sql()} AS is_historical_model,
                       a.created_at AS analyzed_at
                FROM photos p JOIN libraries l ON l.id=p.library_id
                LEFT JOIN photo_analysis a ON a.id=(SELECT display.id FROM photo_analysis display WHERE display.photo_id=p.id ORDER BY {display_analysis_order_sql('display')} LIMIT 1)
                WHERE {where} ORDER BY COALESCE(p.captured_at,p.created_at) DESC,p.id LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
        return rows, total

    def score_population(self, library_id: str | None = None) -> list[float]:
        """Pure read of active, eligible Vision v4 semantic scores for one library."""
        global _SCORE_POPULATION_CACHE
        cache_key = f"{self.database.path}:{library_id or 'all'}"
        now = time.monotonic()
        with _SCORE_POPULATION_LOCK:
            if (
                _SCORE_POPULATION_CACHE is not None
                and _SCORE_POPULATION_CACHE[0] == cache_key
                and now - _SCORE_POPULATION_CACHE[1] < _SCORE_POPULATION_TTL_SECONDS
            ):
                return list(_SCORE_POPULATION_CACHE[2])
            with self.database.session() as connection:
                rows = connection.execute(
                    f"""
                    SELECT a.ranking_score
                    FROM photo_analysis a
                    JOIN photos p ON p.id=a.photo_id
                    JOIN libraries l ON l.id=p.library_id
                    WHERE a.ranking_score IS NOT NULL AND a.schema_version=4
                      AND a.score_kind=?
                      AND p.eligible=1 AND p.exclusion_status NOT IN ('auto_excluded','manually_excluded')
                      AND p.lifecycle_status='active' AND l.enabled=1
                      AND (? IS NULL OR p.library_id=?)
                      AND a.id=(
                        SELECT latest.id FROM photo_analysis latest
                        WHERE latest.photo_id=a.photo_id
                        ORDER BY {preferred_analysis_order_sql('latest')} LIMIT 1
                      )
                    """, (SEMANTIC_SCORE_KIND, library_id, library_id)
                ).fetchall()
            values = tuple(float(row["ranking_score"]) for row in rows)
            _SCORE_POPULATION_CACHE = (cache_key, time.monotonic(), values)
            return list(values)

    @staticmethod
    def _local_quality_settings(connection) -> dict:
        rows = connection.execute("SELECT key,value_json FROM settings WHERE key IN ('analysis.prefilter_enabled','analysis.prefilter_screenshots','analysis.prefilter_low_quality','analysis.prefilter_sensitivity')").fetchall()
        return {row["key"]: json.loads(row["value_json"]) for row in rows} | {"analysis.e6_prefilter_enabled": False}

    @staticmethod
    def _content_settings(connection) -> dict:
        rows = connection.execute("SELECT key,value_json FROM settings WHERE key IN (?,?,?,?,?)", tuple(CONTENT_FILTER_DEFAULTS)).fetchall()
        return CONTENT_FILTER_DEFAULTS | {row["key"]: json.loads(row["value_json"]) for row in rows}

    @staticmethod
    def _apply_content_exclusion(connection, photo_id, evaluation, now) -> None:
        connection.execute(
            "UPDATE photos SET eligible=0,exclusion_status='auto_excluded',reject_reason=?,reject_rule=?,reject_rule_version=?,reject_details_json=?,rejected_at=?,updated_at=? WHERE id=?",
            (evaluation["primary_reason"], evaluation["rule"], evaluation["rule_version"], json.dumps(evaluation), now, now, photo_id),
        )
        connection.execute("INSERT INTO photo_events(photo_id,event,changes_json,created_at) VALUES (?,'automatic_exclusion',?,?)", (photo_id, json.dumps(evaluation), now))

    @staticmethod
    def _mark_library_ranking_dirty(connection, library_id: str, now: str | None = None) -> None:
        timestamp = now or datetime.now(timezone.utc).isoformat()
        connection.execute(
            """INSERT INTO library_ranking_state(library_id,dirty,updated_at)
               VALUES (?,1,?)
               ON CONFLICT(library_id) DO UPDATE SET dirty=1,updated_at=excluded.updated_at""",
            (library_id, timestamp),
        )

    def ranking_state(self, library_id: str) -> dict | None:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT library_id,dirty,updated_at,last_refreshed_at FROM library_ranking_state WHERE library_id=?",
                (library_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def refresh_library_ranking(self, library_id: str, *, connection=None) -> bool:
        """Refresh one dirty library once; a failure leaves its durable flag set."""
        context = self.database.transaction() if connection is None else nullcontext(connection)
        with context as active_connection:
            row = active_connection.execute(
                "SELECT dirty FROM library_ranking_state WHERE library_id=?", (library_id,)
            ).fetchone()
            if row is None or not bool(row["dirty"]):
                return False
            self._refresh_library_ranking(active_connection, library_id)
            now = datetime.now(timezone.utc).isoformat()
            active_connection.execute(
                "UPDATE library_ranking_state SET dirty=0,updated_at=?,last_refreshed_at=? WHERE library_id=?",
                (now, now, library_id),
            )
        invalidate_score_population_cache()
        return True

    def refresh_dirty_libraries_for_job(self, job_id: str, *, connection=None) -> int:
        """Refresh each library touched by a completed Job at most once."""
        context = self.database.transaction() if connection is None else nullcontext(connection)
        with context as active_connection:
            rows = active_connection.execute(
                """SELECT DISTINCT p.library_id
                   FROM job_items ji
                   JOIN photos p ON p.id=ji.photo_id
                   JOIN library_ranking_state state ON state.library_id=p.library_id
                   WHERE ji.job_id=? AND state.dirty=1
                   ORDER BY p.library_id""",
                (job_id,),
            ).fetchall()
            refreshed = 0
            for row in rows:
                refreshed += int(
                    self.refresh_library_ranking(str(row["library_id"]), connection=active_connection)
                )
        return refreshed

    @staticmethod
    def _refresh_library_ranking(connection, library_id: str) -> None:
        """Recompute current semantic rows with the AI-first ranking contract."""
        peers = """SELECT a.*,p.favorite FROM photos p
            JOIN photo_analysis a ON a.id=(SELECT id FROM photo_analysis WHERE photo_id=p.id AND schema_version=4 AND score_kind=? ORDER BY created_at DESC,id DESC LIMIT 1)
            WHERE p.library_id=? AND p.eligible=1 AND p.lifecycle_status='active'
              AND p.exclusion_status NOT IN ('auto_excluded','manually_excluded')"""
        cursor = connection.execute(peers, (SEMANTIC_SCORE_KIND, library_id))
        while batch := cursor.fetchmany(500):
            updates = []
            for row in batch:
                analysis = dict(row)
                scores = ranking_components(analysis, favorite=bool(row["favorite"]))
                if (
                    row["library_rarity_adjustment"] != 0
                    or scores["ranking_score"] != row["ranking_score"]
                    or scores["effective_special_level"] != row["effective_special_level"]
                    or scores["base_ranking_score"] != row["base_ranking_score"]
                    or row["ranking_rule_version"] != RANKING_RULE_VERSION
                ):
                    updates.append((
                        0, scores["effective_special_level"], scores["ranking_score"],
                        scores["ranking_score"], scores["base_ranking_score"],
                        scores["base_ranking_score"], RANKING_RULE_VERSION, row["id"],
                    ))
            connection.executemany(
                """UPDATE photo_analysis SET library_rarity_adjustment=?,effective_special_level=?,
                   ranking_score=?,final_ranking_score=?,base_ranking_score=?,semantic_score=?,
                   ranking_rule_version=? WHERE id=?""",
                updates,
            )

    @staticmethod
    def _refresh_favorite_ranking(connection, photo_id: str) -> None:
        row = connection.execute(
            "SELECT a.*,p.favorite FROM photo_analysis a JOIN photos p ON p.id=a.photo_id "
            "WHERE a.photo_id=? AND a.schema_version=4 AND a.score_kind=? "
            "ORDER BY a.created_at DESC,a.id DESC LIMIT 1",
            (photo_id, SEMANTIC_SCORE_KIND),
        ).fetchone()
        if row is None:
            return
        scores = ranking_components(dict(row), favorite=bool(row["favorite"]))
        connection.execute(
            """UPDATE photo_analysis SET ranking_score=?,final_ranking_score=?,base_ranking_score=?,
               semantic_score=?,effective_special_level=?,library_rarity_adjustment=0,
               ranking_rule_version=? WHERE id=?""",
            (scores["ranking_score"], scores["ranking_score"], scores["base_ranking_score"],
             scores["base_ranking_score"], scores["effective_special_level"], RANKING_RULE_VERSION, row["id"]),
        )

    def save_analysis(
        self,
        photo_id: str,
        job_id: str | None,
        stage: str,
        provider: str,
        model: str,
        result: dict,
        raw_json: str,
        analysis_source: str = "direct",
        *,
        ranking_score: float | None = None,
        scoring_version_id: str | None = None,
        schema_kind: str = "basic",
        score_kind: str | None = None,
        local_score: float | None = None,
        semantic_score: float | None = None,
        base_ranking_score: float | None = None,
        final_ranking_score: float | None = None,
        prompt_version: str = "photo-quality-v3",
        analysis_fingerprint: str | None = None,
        analysis_spec_json: str | None = None,
        vision_request_fingerprint: str | None = None,
        vision_input_spec_json: str | None = None,
        prefilter_evaluation: dict | None = None,
        inherited_from: dict | None = None,
        connection=None,
    ) -> None:
        import json

        now = datetime.now(timezone.utc).isoformat()
        context = self.database.session() if connection is None else nullcontext(connection)
        with context as active_connection:
            own_transaction = connection is None
            connection = active_connection
            if own_transaction:
                connection.execute("BEGIN IMMEDIATE")
            try:
                score_kind = resolve_score_kind(score_kind, provider=provider, stage=stage)
                if prefilter_evaluation and prefilter_evaluation.get("decision") == "auto_excluded":
                    current = connection.execute(
                        "SELECT favorite,manual_override,exclusion_status FROM photos WHERE id=?", (photo_id,)
                    ).fetchone()
                    protected = (
                        current is None
                        or bool(current["manual_override"])
                        or str(current["exclusion_status"] or "")
                        in {"manually_restored", "manually_excluded"}
                    )
                    if protected:
                        event = "automatic_exclusion_skipped"
                        changes = {"reason": "manual_override"}
                    else:
                        details = json.dumps(prefilter_evaluation, ensure_ascii=False, sort_keys=True)
                        connection.execute(
                            """UPDATE photos SET eligible=0,exclusion_status='auto_excluded',reject_reason=?,
                               reject_rule=?,reject_rule_version=?,reject_details_json=?,rejected_at=?,updated_at=? WHERE id=?""",
                            (
                                prefilter_evaluation["primary_reason"],
                                LOCAL_QUALITY_RULE,
                                QUALITY_POLICY_VERSION,
                                details,
                                now,
                                now,
                                photo_id,
                            ),
                        )
                        event = "automatic_exclusion"
                        changes = {
                            "reason": prefilter_evaluation["primary_reason"],
                            "feature_version": FEATURE_VERSION,
                        }
                    connection.execute(
                        "INSERT INTO photo_events(photo_id,event,changes_json,changed_by,created_at) VALUES (?,?,?,?,?)",
                        # Automatic policy decisions have no authenticated user;
                        # `changed_by` is a foreign key and must remain NULL
                        # rather than inventing a non-existent system account.
                        (photo_id, event, json.dumps(changes, ensure_ascii=False), None, now),
                    )
                canonical = validate_analysis_result({key: result[key] for key in REQUIRED_FIELDS})
                current = dict(connection.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone())
                content_evaluation = evaluate_content_filter(canonical["content_filter"], self._content_settings(connection))
                protected = bool(current["manual_override"]) or current["exclusion_status"] in {"manually_restored", "manually_excluded"}
                if provider != "local" and content_evaluation["decision"] == "auto_excluded" and not protected:
                    self._apply_content_exclusion(connection, photo_id, content_evaluation, now)
                    current["eligible"] = 0
                    current["exclusion_status"] = "auto_excluded"
                quality_evaluation = evaluate_local_quality(current, settings=self._local_quality_settings(connection))
                if current["eligible"] and quality_evaluation["decision"] == "auto_excluded" and not protected:
                    self._apply_content_exclusion(connection, photo_id, {
                        **quality_evaluation, "rule": LOCAL_QUALITY_RULE,
                        "rule_version": QUALITY_POLICY_VERSION,
                    }, now)
                    current["eligible"] = 0
                    current["exclusion_status"] = "auto_excluded"
                quality = local_candidate_score(current, evaluation=quality_evaluation)
                rarity = 0
                result.update(canonical, local_quality_score=quality)
                semantic_available = score_kind == SEMANTIC_SCORE_KIND
                if semantic_available:
                    result.update(
                        ranking_components(
                            result,
                            favorite=bool(current["favorite"]),
                        )
                    )
                else:
                    result.update(
                        base_ranking_score=None,
                        raw_ranking_score=None,
                        ranking_score=None,
                        final_ranking_score=None,
                        effective_special_level=None,
                    )
                result["local_score"] = quality
                result["selection_score"] = (
                    result["ranking_score"]
                    if semantic_available
                    and current["eligible"]
                    and current["exclusion_status"] not in EXCLUDED_STATUSES
                    else quality
                    if not semantic_available
                    else 0.0
                )
                semantic = {
                    "source": "model" if semantic_available else "local",
                    "score_kind": score_kind,
                    "semantic_scores_available": semantic_available,
                    "local_quality": quality_evaluation,
                    "values": (
                        {
                            key: canonical[key]
                            for key in (
                                "people_count",
                                "subject_position",
                                "text_safe_area",
                                "special_level",
                                "special_codes",
                                "content_filter",
                            )
                        }
                        if semantic_available
                        else {}
                    ),
                    **({"inherited_from": inherited_from} if inherited_from else {}),
                }
                record = {
                    "photo_id": photo_id, "job_id": job_id, "schema_version": 4,
                    "stage": stage, "provider": provider, "model": model,
                    "caption": canonical["caption"], "types_json": json.dumps(canonical["types"], ensure_ascii=False),
                    "memory_score": canonical["memory_score"] if semantic_available else None,
                    "visual_score": canonical["visual_score"] if semantic_available else None,
                    "local_quality_score": quality, "special_level": canonical["special_level"],
                    "special_codes_json": json.dumps(canonical["special_codes"]), "people_count": canonical["people_count"],
                    "content_filter_json": json.dumps(canonical["content_filter"]),
                    "effective_special_level": result["effective_special_level"],
                    "library_rarity_adjustment": rarity,
                    "side_caption": canonical["side_caption"], "raw_json": json.dumps(canonical, ensure_ascii=False),
                    "analysis_source": analysis_source,
                    "ranking_score": result["ranking_score"],
                    "scoring_version_id": scoring_version_id if semantic_available else None,
                    "created_at": now,
                    "schema_kind": schema_kind,
                    "score_kind": score_kind,
                    "semantic_json": json.dumps(semantic, ensure_ascii=False), "local_score": quality,
                    "semantic_score": result["base_ranking_score"] if semantic_available else None,
                    "base_ranking_score": result["base_ranking_score"], "final_ranking_score": result["ranking_score"],
                    "ranking_rule_version": RANKING_RULE_VERSION, "prompt_version": prompt_version,
                    "analysis_fingerprint": analysis_fingerprint, "analysis_spec_json": analysis_spec_json,
                    "vision_request_fingerprint": vision_request_fingerprint, "vision_input_spec_json": vision_input_spec_json,
                }
                columns = ",".join(record)
                placeholders = ",".join("?" for _ in record)
                connection.execute(f"INSERT INTO photo_analysis ({columns}) VALUES ({placeholders})", tuple(record.values()))  # noqa: S608 -- fixed internal column names
                if semantic_available or (
                    prefilter_evaluation
                    and prefilter_evaluation.get("decision") == "auto_excluded"
                ):
                    self._mark_library_ranking_dirty(connection, current["library_id"], now)
                connection.execute(
                    "UPDATE photos SET status='analyzed',updated_at=? WHERE id=?", (now, photo_id)
                )
                # Local quality/fallback rows have no visual model judgement and must
                # never erase the latest provider result.
                is_provider_result = score_kind == SEMANTIC_SCORE_KIND
                if is_provider_result:
                    visual = result.get("visual_orientation") or {}
                    connection.execute(
                        "UPDATE photos SET exif_orientation_original=COALESCE(exif_orientation_original,orientation),visual_orientation_rotation_cw=?,visual_orientation_confidence=?,visual_orientation_ambiguous=?,visual_orientation_evidence_json=?,updated_at=? WHERE id=?",
                        (
                            visual.get("rotation_cw"),
                            visual.get("confidence"),
                            int(bool(visual.get("ambiguous", True))),
                            json.dumps(visual.get("evidence", []), ensure_ascii=False),
                            now,
                            photo_id,
                        ),
                    )
                if own_transaction:
                    connection.execute("COMMIT")
            except Exception:
                if own_transaction and connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        invalidate_score_population_cache()

    def set_manual_orientation(self, photo_id: str, rotation_cw: int | None, changed_by: str) -> dict:
        if isinstance(rotation_cw, bool) or rotation_cw not in {0, 90, 180, 270, None}:
            raise ValueError("旋轉角度不合法")
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            row = connection.execute("SELECT id FROM photos WHERE id=?", (photo_id,)).fetchone()
            if row is None:
                raise KeyError(photo_id)
            connection.execute(
                "UPDATE photos SET manual_orientation_rotation_cw=?,manual_orientation_updated_at=?,manual_orientation_updated_by=?,updated_at=? WHERE id=?",
                (
                    rotation_cw,
                    now if rotation_cw is not None else None,
                    changed_by if rotation_cw is not None else None,
                    now,
                    photo_id,
                ),
            )
            connection.execute(
                "INSERT INTO photo_events(photo_id,event,changes_json,changed_by,created_at) VALUES (?,'manual_orientation',?,?,?)",
                (photo_id, json.dumps({"rotation_cw": rotation_cw}, ensure_ascii=False), changed_by, now),
            )
        return {"rotation_cw": rotation_cw}
