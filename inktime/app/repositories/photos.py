from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Sequence
from uuid import uuid4

from inktime.app.core.paths import UnsafePathError, safe_join
from inktime.app.db import Database
from inktime.app.domain.analysis.scoring import (
    DEFAULT_FAVORITE_BONUS,
    DEFAULT_RANKING_WEIGHTS,
    calculate_ranking_score,
)
from inktime.app.domain.photos.preprocessing import LocalPhotoFeatures


LOCAL_QUALITY_RULE = "local-quality"
LOCAL_QUALITY_RULE_VERSION = "local-quality-v3"


def _local_candidate_score(features: LocalPhotoFeatures) -> float:
    """不需要模型的可選片分數；只描述技術可用性，不代替回憶或語意評分。"""
    blur = max(0.0, float(features.blur_score or 0.0))
    contrast = max(0.0, min(100.0, float(features.contrast or 0.0)))
    exposure_penalty = max(
        float(features.overexposed_ratio or 0.0), float(features.underexposed_ratio or 0.0)
    )
    short_edge = min(features.width, features.height)
    resolution = min(12.0, short_edge / 100.0)
    return round(max(0.0, min(100.0, blur**0.5 * 3.2 + contrast * 0.8 + resolution - exposure_penalty * 45)), 2)


def _automatic_exclusion(relative_path: str, features: LocalPhotoFeatures) -> tuple[str, dict] | None:
    """回傳可重現的本機排除證據；不得在此處呼叫 Provider。"""
    filename = Path(relative_path).name.casefold()
    document_markers = ("receipt", "invoice", "document", "scan", "收據", "發票", "文件")
    if any(marker in filename for marker in document_markers):
        return "document_or_receipt", {"measured_value": filename, "threshold": "filename marker"}
    screenshot = float(features.screenshot_likelihood or 0.0)
    if screenshot >= 0.65:
        return "screenshot", {"measured_value": round(screenshot, 3), "threshold": 0.65}
    short_edge = min(features.width, features.height)
    if short_edge < 240:
        return "resolution_too_low", {"measured_value": short_edge, "threshold": 240}
    blur = float(features.blur_score or 0.0)
    contrast = float(features.contrast or 0.0)
    if blur < 8 and contrast < 12 and short_edge < 600:
        return "blur_too_high", {
            "measured_value": round(blur, 2),
            "threshold": 8.0,
            "secondary_threshold": "short_edge < 600",
        }
    overexposed = float(features.overexposed_ratio or 0.0)
    underexposed = float(features.underexposed_ratio or 0.0)
    if overexposed >= 0.7:
        return "overexposed", {"measured_value": round(overexposed, 4), "threshold": 0.7}
    if underexposed >= 0.7:
        return "underexposed", {"measured_value": round(underexposed, 4), "threshold": 0.7}
    return None


def _stored_exclusion(photo: dict) -> tuple[str, dict] | None:
    """讓人工要求重新套用時使用同一規則與相同門檻。"""
    features = LocalPhotoFeatures(
        sha256=str(photo.get("sha256") or ""),
        perceptual_hash=photo.get("perceptual_hash"),
        difference_hash=photo.get("difference_hash"),
        width=int(photo.get("width") or 0),
        height=int(photo.get("height") or 0),
        format=str(photo.get("format") or ""),
        orientation=int(photo.get("orientation") or 1),
        camera_make=photo.get("camera_make"),
        camera_model=photo.get("camera_model"),
        lens_model=photo.get("lens_model"),
        exif_json=photo.get("exif_json"),
        captured_at=photo.get("captured_at"),
        gps_lat=photo.get("gps_lat"),
        gps_lon=photo.get("gps_lon"),
        brightness=photo.get("brightness"),
        contrast=photo.get("contrast"),
        blur_score=photo.get("blur_score"),
        overexposed_ratio=photo.get("overexposed_ratio"),
        underexposed_ratio=photo.get("underexposed_ratio"),
        screenshot_likelihood=photo.get("screenshot_likelihood"),
        crop_focus_x=None,
        crop_focus_y=None,
        crop_subject_left=None,
        crop_subject_top=None,
        crop_subject_right=None,
        crop_subject_bottom=None,
        crop_method=None,
        crop_face_count=None,
        e6_score=None,
        e6_contrast_score=None,
        e6_subject_score=None,
        e6_skin_score=None,
        e6_text_score=None,
        e6_skin_pixels=None,
    )
    return _automatic_exclusion(str(photo.get("relative_path") or ""), features)


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
                               metadata_status,local_features_status,status
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
            connection.executemany(
                """
                UPDATE photos SET
                    last_seen_scan_id=?,
                    lifecycle_status=CASE WHEN lifecycle_status='missing' THEN 'active' ELSE lifecycle_status END,
                    missing_since=CASE WHEN lifecycle_status='missing' THEN NULL ELSE missing_since END,
                    missing_reason=CASE WHEN lifecycle_status='missing' THEN NULL ELSE missing_reason END,
                    updated_at=CASE WHEN lifecycle_status='missing' THEN ? ELSE updated_at END
                WHERE id=?
                """,
                [(scan_id, now, photo_id) for photo_id in unique_ids],
            )

    def mark_processing_failed_batch(
        self, scan_id: str, failures: Sequence[tuple[str, bool, bool]]
    ) -> None:
        """保留既有照片資料，但把本次未完成區段標成 failed 供增量重試。"""

        if not failures:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            connection.executemany(
                """
                UPDATE photos SET
                    last_seen_scan_id=?,
                    lifecycle_status=CASE WHEN lifecycle_status='missing' THEN 'active' ELSE lifecycle_status END,
                    missing_since=CASE WHEN lifecycle_status='missing' THEN NULL ELSE missing_since END,
                    missing_reason=CASE WHEN lifecycle_status='missing' THEN NULL ELSE missing_reason END,
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
    ) -> list[BatchPhotoResult]:
        """批次查詢、記憶體比對，再於單一交易寫入整批照片與初始狀態。"""

        if not items:
            return []
        now = datetime.now(timezone.utc).isoformat()
        paths = list(dict.fromkeys(item.relative_path for item in items))
        hashes = list(dict.fromkeys(item.features.sha256 for item in items))
        phashes = list(
            dict.fromkeys(
                item.features.perceptual_hash
                for item in items
                if item.features.perceptual_hash is not None
            )
        )
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
                        and not self._path_is_still_present(root, str(row["relative_path"]))
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
                eligible_exact = [
                    row for row in exact if str(row["id"]) not in reserved_move_ids
                ]
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
                    action = (
                        "restored"
                        if str(existing["lifecycle_status"]) == "missing"
                        else "changed"
                    )
                    duplicate_group = (
                        existing.get("duplicate_group_id") if not content_changed else None
                    )
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
                        duplicate_source.get("duplicate_group_id")
                        or duplicate_group
                        or str(uuid4())
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
                            exif_json=NULL,captured_at=NULL,gps_lat=NULL,gps_lon=NULL,
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

            # 本機品質決策屬於掃描產物，不等待也不觸發大型模型。已人工處理的照片
            # 只在內容 SHA 改變時才重新套用規則，避免「恢復後立刻又被排除」。
            quality_updates = []
            for plan in plans:
                features = plan["item"].features
                if not features.local_features_complete:
                    continue
                existing = plan["existing"]
                protected = bool(existing) and not plan["content_changed"] and str(
                    existing.get("exclusion_status") or ""
                ) in {"manually_restored", "manually_excluded"}
                exclusion = None if protected else _automatic_exclusion(plan["item"].relative_path, features)
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
                        _local_candidate_score(features),
                        LOCAL_QUALITY_RULE_VERSION,
                        features.orientation,
                        features.camera_make,
                        features.camera_model,
                        features.lens_model,
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
                        camera_make=?,camera_model=?,lens_model=?,eligible=?,exclusion_status=?,
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

    def inherit_existing_analysis(self, photo_id: str, job_id: str | None) -> dict | None:
        with self.database.session() as connection:
            row = connection.execute(
                """
                SELECT a.* FROM photos target
                JOIN photos source ON source.sha256=target.sha256 AND source.id<>target.id
                JOIN photo_analysis a ON a.photo_id=source.id
                WHERE target.id=? ORDER BY a.created_at DESC LIMIT 1
                """,
                (photo_id,),
            ).fetchone()
        if row is None:
            return None
        import json

        result = {
            "schema_version": row["schema_version"],
            "caption": row["caption"],
            "types": json.loads(row["types_json"]),
            "memory_score": row["memory_score"],
            "beauty_score": row["beauty_score"],
            "technical_quality_score": row["technical_quality_score"],
            "emotion_score": row["emotion_score"],
            "side_caption": row["side_caption"],
            "should_keep": bool(row["should_keep"]),
            "sensitive": bool(row["sensitive"]),
            "reason": row["reason"],
            "ranking_score": row["ranking_score"],
        }
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
        )
        return result

    def get_with_path(self, photo_id: str):
        with self.database.session() as connection:
            return connection.execute(
                """
                SELECT p.*, l.root_path FROM photos p JOIN libraries l ON l.id=p.library_id WHERE p.id=?
                """,
                (photo_id,),
            ).fetchone()

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
                elif action in {"favorite", "candidate"}:
                    exclusion_status = "manually_restored" if action == "favorite" else "pending_review"
                    connection.execute(
                        """
                        UPDATE photos SET favorite=?,eligible=1,exclusion_status=?,manual_override=1,
                            updated_at=? WHERE id=?
                        """,
                        (int(action == "favorite"), exclusion_status, now, photo_id),
                    )
                    event = "added_to_favorites" if action == "favorite" else "added_to_candidate_pool"
                    changes = {"eligible": True, "favorite": action == "favorite", "candidate_pool": action == "candidate"}
                elif action == "reanalyze":
                    exclusion = _stored_exclusion(photo)
                    protected = bool(photo.get("manual_override")) and not reapply_rules
                    if protected:
                        connection.execute(
                            "UPDATE photos SET local_candidate_score=?,feature_version=?,updated_at=? WHERE id=?",
                            (
                                _local_candidate_score(
                                    LocalPhotoFeatures(
                                        sha256=str(photo.get("sha256") or ""), perceptual_hash=None,
                                        difference_hash=None, width=int(photo.get("width") or 0),
                                        height=int(photo.get("height") or 0), format=str(photo.get("format") or ""),
                                        orientation=int(photo.get("orientation") or 1), camera_make=None,
                                        camera_model=None, lens_model=None, exif_json=None,
                                        captured_at=None, gps_lat=None, gps_lon=None,
                                        brightness=photo.get("brightness"), contrast=photo.get("contrast"),
                                        blur_score=photo.get("blur_score"),
                                        overexposed_ratio=photo.get("overexposed_ratio"),
                                        underexposed_ratio=photo.get("underexposed_ratio"),
                                        screenshot_likelihood=photo.get("screenshot_likelihood"),
                                        crop_focus_x=None, crop_focus_y=None, crop_subject_left=None,
                                        crop_subject_top=None, crop_subject_right=None, crop_subject_bottom=None,
                                        crop_method=None, crop_face_count=None, e6_score=None,
                                        e6_contrast_score=None, e6_subject_score=None, e6_skin_score=None,
                                        e6_text_score=None, e6_skin_pixels=None,
                                    )
                                ),
                                LOCAL_QUALITY_RULE_VERSION,
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
                            (LOCAL_QUALITY_RULE_VERSION, now, photo_id),
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
                            (reason, LOCAL_QUALITY_RULE, LOCAL_QUALITY_RULE_VERSION, details, now, LOCAL_QUALITY_RULE_VERSION, now, photo_id),
                        )
                    event = "local_reanalysis"
                    changes = {"reapply_rules": reapply_rules, "manual_override": not reapply_rules and bool(photo.get("manual_override"))}
                else:
                    raise ValueError("不支援的排除操作")
                connection.execute(
                    "INSERT INTO photo_events(photo_id,event,changes_json,changed_by,created_at) VALUES (?,?,?,?,?)",
                    (photo_id, event, json.dumps(changes, ensure_ascii=False), changed_by, now),
                )
                result = dict(connection.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone())
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

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
        clauses = ["p.exclusion_status != 'eligible'"]
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
            clauses.append("p.exclusion_status IN ('manually_excluded','manually_restored')")
        elif origin == "auto":
            clauses.append("p.exclusion_status='auto_excluded'")
        where = " AND ".join(clauses)
        with self.database.session() as connection:
            return connection.execute(
                f"""
                SELECT p.*,l.name AS library_name,a.provider,a.model,a.created_at AS analyzed_at
                FROM photos p JOIN libraries l ON l.id=p.library_id
                LEFT JOIN photo_analysis a ON a.id=(
                    SELECT id FROM photo_analysis WHERE photo_id=p.id ORDER BY created_at DESC,id DESC LIMIT 1
                )
                WHERE {where}
                ORDER BY p.rejected_at DESC,p.updated_at DESC,p.id DESC LIMIT ?
                """,
                (*parameters, max(1, min(int(limit), 500))),
            ).fetchall()

    def eligible_photo_ids(self, *, limit: int | None = None, include_all_active: bool = False) -> list[str]:
        where = "p.lifecycle_status='active'"
        if not include_all_active:
            where += " AND p.eligible=1"
        query = f"SELECT p.id FROM photos p WHERE {where} ORDER BY p.local_candidate_score DESC,p.captured_at DESC,p.id"
        params: tuple = () if limit is None else (max(1, min(int(limit), 100_000)),)
        if limit is not None:
            query += " LIMIT ?"
        with self.database.session() as connection:
            return [str(row["id"]) for row in connection.execute(query, params).fetchall()]

    def eligible_photo_batches(
        self, *, group_by: str, limit: int, include_all_active: bool = False
    ) -> list[tuple[str, list[str]]]:
        """完整照片庫模式以年份或第一層資料夾拆成可暫停／續跑的既有工作。"""
        where = "lifecycle_status='active'" if include_all_active else "lifecycle_status='active' AND eligible=1"
        with self.database.session() as connection:
            rows = connection.execute(
                f"""
                SELECT id,relative_path,captured_at,created_at FROM photos WHERE {where}
                ORDER BY COALESCE(captured_at,created_at),relative_path,id
                """
            ).fetchall()
        groups: dict[str, list[str]] = {}
        remaining = max(1, min(int(limit), 100_000))
        for row in rows:
            if remaining <= 0:
                break
            path = str(row["relative_path"] or "")
            key = (
                path.split("/", 1)[0] or "根目錄"
                if group_by == "folder"
                else str(row["captured_at"] or row["created_at"] or "未知")[:4]
            )
            groups.setdefault(key or "未知", []).append(str(row["id"]))
            remaining -= 1
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
                    (float(latitude) - delta, float(latitude) + delta, float(longitude) - delta, float(longitude) + delta),
                ).fetchone()[0]
            )

    def get_ai_cache(
        self, *, content_sha256: str, provider: str, model_name: str, prompt_version: str, schema_version: int, schema_kind: str
    ) -> dict | None:
        with self.database.session() as connection:
            row = connection.execute(
                """
                SELECT * FROM ai_analysis_cache WHERE content_sha256=? AND provider=? AND model_name=?
                  AND prompt_version=? AND schema_version=? AND schema_kind=?
                """,
                (content_sha256, provider, model_name, prompt_version, schema_version, schema_kind),
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
    ) -> None:
        with self.database.session() as connection:
            connection.execute(
                """
                INSERT INTO ai_analysis_cache(content_sha256,provider,model_name,prompt_version,schema_version,schema_kind,
                    result_json,raw_json,input_tokens,output_tokens,cached_tokens,estimated_cost,latency_ms,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(content_sha256,provider,model_name,prompt_version,schema_version,schema_kind)
                DO UPDATE SET result_json=excluded.result_json,raw_json=excluded.raw_json,input_tokens=excluded.input_tokens,
                    output_tokens=excluded.output_tokens,cached_tokens=excluded.cached_tokens,estimated_cost=excluded.estimated_cost,
                    latency_ms=excluded.latency_ms,created_at=excluded.created_at
                """,
                (
                    content_sha256, provider, model_name, prompt_version, schema_version, schema_kind,
                    json.dumps(result, ensure_ascii=False), raw_json, input_tokens, output_tokens, cached_tokens,
                    estimated_cost, latency_ms, datetime.now(timezone.utc).isoformat(),
                ),
            )

    def list_existing_photo_ids(
        self, library_id: str, root: Path, *, limit: int
    ) -> list[str]:
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
        changes = {
            "favorite": favorite,
            "captured_at": captured_at,
            "types": types,
            "side_caption": side_caption,
        }
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE photos SET favorite=?,captured_at=?,updated_at=? WHERE id=?",
                    (int(favorite), captured_at or None, now, photo_id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(photo_id)
                latest = connection.execute(
                    "SELECT id FROM photo_analysis WHERE photo_id=? ORDER BY created_at DESC LIMIT 1",
                    (photo_id,),
                ).fetchone()
                if latest:
                    connection.execute(
                        "UPDATE photo_analysis SET types_json=?,side_caption=? WHERE id=?",
                        (json.dumps(types, ensure_ascii=False), side_caption, latest["id"]),
                    )
                    analysis = connection.execute(
                        """
                        SELECT a.memory_score,a.beauty_score,a.technical_quality_score,
                               a.emotion_score,v.memory_weight,v.beauty_weight,
                               v.technical_weight,v.emotion_weight,v.favorite_bonus
                        FROM photo_analysis a
                        LEFT JOIN scoring_rule_versions v ON v.id=a.scoring_version_id
                        WHERE a.id=?
                        """,
                        (latest["id"],),
                    ).fetchone()
                    weights = {
                        "memory": float(
                            analysis["memory_weight"]
                            if analysis["memory_weight"] is not None
                            else DEFAULT_RANKING_WEIGHTS["memory"]
                        ),
                        "beauty": float(
                            analysis["beauty_weight"]
                            if analysis["beauty_weight"] is not None
                            else DEFAULT_RANKING_WEIGHTS["beauty"]
                        ),
                        "technical_quality": float(
                            analysis["technical_weight"]
                            if analysis["technical_weight"] is not None
                            else DEFAULT_RANKING_WEIGHTS["technical_quality"]
                        ),
                        "emotion": float(
                            analysis["emotion_weight"]
                            if analysis["emotion_weight"] is not None
                            else DEFAULT_RANKING_WEIGHTS["emotion"]
                        ),
                    }
                    ranking_score = calculate_ranking_score(
                        analysis,
                        weights,
                        favorite=favorite,
                        favorite_bonus=float(
                            analysis["favorite_bonus"]
                            if analysis["favorite_bonus"] is not None
                            else DEFAULT_FAVORITE_BONUS
                        ),
                    )
                    connection.execute(
                        "UPDATE photo_analysis SET ranking_score=? WHERE id=?",
                        (ranking_score, latest["id"]),
                    )
                connection.execute(
                    "INSERT INTO photo_events(photo_id,event,changes_json,changed_by,created_at) VALUES (?,'manual_update',?,?,?)",
                    (photo_id, json.dumps(changes, ensure_ascii=False), changed_by, now),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def update_crop(self, photo_id: str, *, manual_x: float | None, manual_y: float | None) -> None:
        if (manual_x is None) != (manual_y is None):
            raise ValueError("裁切 X 與 Y 必須同時設定或同時清除")
        if manual_x is not None and manual_y is not None and not (
            0.0 <= manual_x <= 1.0 and 0.0 <= manual_y <= 1.0
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
            clauses.append("a.memory_score>=?")
            parameters.append(minimum_score)
        if duplicate_only:
            clauses.append("p.duplicate_group_id IS NOT NULL")
        where = " AND ".join(clauses)
        with self.database.session() as connection:
            total = int(
                connection.execute(
                    f"""
                SELECT COUNT(*) FROM photos p
                LEFT JOIN photo_analysis a ON a.id=(SELECT id FROM photo_analysis WHERE photo_id=p.id ORDER BY created_at DESC LIMIT 1)
                WHERE {where}
                """,
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT p.*,l.name AS library_name,a.caption,a.types_json,a.memory_score,a.beauty_score,a.ranking_score,a.side_caption,
                       a.provider,a.model,a.raw_json,a.created_at AS analyzed_at,
                       (SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM api_usage u WHERE u.photo_id=p.id) AS tokens,
                       (SELECT COALESCE(SUM(COALESCE(actual_cost,estimated_cost)),0) FROM api_usage u WHERE u.photo_id=p.id) AS cost
                FROM photos p JOIN libraries l ON l.id=p.library_id
                LEFT JOIN photo_analysis a ON a.id=(SELECT id FROM photo_analysis WHERE photo_id=p.id ORDER BY created_at DESC LIMIT 1)
                WHERE {where} ORDER BY COALESCE(p.captured_at,p.created_at) DESC,p.id LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
        return rows, total

    def score_population(self) -> list[float]:
        """回傳每張照片最新一次有效排序分，供相對鑑別校準使用。"""
        with self.database.session() as connection:
            rows = connection.execute(
                """
                SELECT a.ranking_score
                FROM photo_analysis a
                WHERE a.ranking_score IS NOT NULL
                  AND a.id=(
                    SELECT latest.id FROM photo_analysis latest
                    WHERE latest.photo_id=a.photo_id
                    ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1
                  )
                """
            ).fetchall()
        return [float(row["ranking_score"]) for row in rows]

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
        local_score: float | None = None,
        semantic_score: float | None = None,
        base_ranking_score: float | None = None,
        final_ranking_score: float | None = None,
        travel_bonus: float = 0.0,
        location_rule_version: str | None = None,
    ) -> None:
        import json

        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO photo_analysis(photo_id,job_id,schema_version,stage,provider,model,caption,types_json,
                        memory_score,beauty_score,technical_quality_score,emotion_score,side_caption,should_keep,
                        sensitive,reason,raw_json,analysis_source,ranking_score,scoring_version_id,created_at,
                        schema_kind,semantic_json,local_score,semantic_score,base_ranking_score,final_ranking_score,
                        ranking_rule_version,travel_bonus,location_rule_version)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        photo_id,
                        job_id,
                        result["schema_version"],
                        stage,
                        provider,
                        model,
                        result["caption"],
                        json.dumps(result["types"], ensure_ascii=False),
                        result["memory_score"],
                        result["beauty_score"],
                        result["technical_quality_score"],
                        result["emotion_score"],
                        result["side_caption"],
                        int(result["should_keep"]),
                        int(result["sensitive"]),
                        result["reason"],
                        raw_json,
                        analysis_source,
                        ranking_score,
                        scoring_version_id,
                        now,
                        schema_kind,
                        json.dumps(
                            {
                                "source": "local" if provider == "local" else "model",
                                "confidence": (result.get("details") or {}).get("confidence"),
                                "values": result.get("details") or {},
                            },
                            ensure_ascii=False,
                        ),
                        local_score,
                        semantic_score,
                        base_ranking_score,
                        final_ranking_score if final_ranking_score is not None else ranking_score,
                        "ranking-v2",
                        travel_bonus,
                        location_rule_version,
                    ),
                )
                connection.execute(
                    "UPDATE photos SET status='analyzed',updated_at=? WHERE id=?", (now, photo_id)
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
