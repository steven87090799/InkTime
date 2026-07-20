from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterator
from uuid import uuid4

from inktime.app.core.paths import UnsafePathError, safe_join
from inktime.app.db import Database
from inktime.app.domain.analysis.scoring import (
    DEFAULT_FAVORITE_BONUS,
    DEFAULT_RANKING_WEIGHTS,
    calculate_ranking_score,
)
from inktime.app.domain.photos.preprocessing import LocalPhotoFeatures


@dataclass(frozen=True)
class StoredPhotoSignature:
    file_size: int | None
    modified_time: float | None
    sha256: str | None

    def matches(self, *, file_size: int, modified_time: float) -> bool:
        return bool(self.sha256) and self.file_size == file_size and self.modified_time == modified_time


class PhotoSignatureLookup:
    """掃描期間重用唯讀連線，避免為每張照片重開 SQLite。"""

    def __init__(self, connection: sqlite3.Connection, library_id: str) -> None:
        self.connection = connection
        self.library_id = library_id

    def get(self, relative_path: str) -> StoredPhotoSignature | None:
        row = self.connection.execute(
            """
            SELECT file_size,modified_time,sha256
            FROM photos WHERE library_id=? AND relative_path=?
            """,
            (self.library_id, relative_path),
        ).fetchone()
        if row is None:
            return None
        return StoredPhotoSignature(
            file_size=row["file_size"],
            modified_time=row["modified_time"],
            sha256=row["sha256"],
        )


class PhotoRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ensure_library(self, name: str, root_path: Path) -> str:
        root = str(root_path.expanduser().resolve())
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            row = connection.execute("SELECT id FROM libraries WHERE root_path=?", (root,)).fetchone()
            if row:
                return str(row["id"])
            library_id = str(uuid4())
            connection.execute(
                "INSERT INTO libraries(id,name,root_path,created_at,updated_at) VALUES (?,?,?,?,?)",
                (library_id, name, root, now, now),
            )
            return library_id

    @contextmanager
    def signature_lookup(self, library_id: str) -> Iterator[PhotoSignatureLookup]:
        with self.database.session() as connection:
            yield PhotoSignatureLookup(connection, library_id)

    def upsert_preprocessed(
        self, library_id: str, relative_path: str, source: Path, features: LocalPhotoFeatures
    ) -> tuple[str, bool]:
        now = datetime.now(timezone.utc).isoformat()
        stat = source.stat()
        values = features.as_dict()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                path_existing = connection.execute(
                    """
                    SELECT id,sha256,status,analysis_source,duplicate_group_id
                    FROM photos
                    WHERE library_id=? AND relative_path=?
                    """,
                    (library_id, relative_path),
                ).fetchone()
                same_content = connection.execute(
                    """
                    SELECT p.id,p.relative_path,p.duplicate_group_id,l.root_path
                    FROM photos p JOIN libraries l ON l.id=p.library_id
                    WHERE p.library_id=? AND p.sha256=? AND p.id<>COALESCE(?, '')
                    ORDER BY p.created_at LIMIT 1
                    """,
                    (library_id, features.sha256, path_existing["id"] if path_existing else None),
                ).fetchone()
                inherited = same_content is not None

                if path_existing is not None:
                    photo_id = str(path_existing["id"])
                    content_changed = path_existing["sha256"] != features.sha256
                    next_status = (
                        "analyzed"
                        if not content_changed and path_existing["status"] == "analyzed"
                        else "preprocessed"
                    )
                    next_analysis_source = (
                        path_existing["analysis_source"]
                        if not content_changed
                        else "inherited" if same_content else "direct"
                    )
                    duplicate = same_content or connection.execute(
                        """
                        SELECT id,duplicate_group_id FROM photos
                        WHERE perceptual_hash=? AND id<>? LIMIT 1
                        """,
                        (features.perceptual_hash, photo_id),
                    ).fetchone()
                    if duplicate:
                        duplicate_group = (
                            duplicate["duplicate_group_id"]
                            or (path_existing["duplicate_group_id"] if not content_changed else None)
                            or str(uuid4())
                        )
                        if not duplicate["duplicate_group_id"]:
                            connection.execute(
                                "UPDATE photos SET duplicate_group_id=? WHERE id=?",
                                (duplicate_group, duplicate["id"]),
                            )
                    else:
                        duplicate_group = (
                            None if content_changed else path_existing["duplicate_group_id"]
                        )
                    connection.execute(
                        """
                        UPDATE photos SET
                            file_size=?,modified_time=?,sha256=?,perceptual_hash=?,difference_hash=?,
                            width=?,height=?,format=?,status=?,duplicate_group_id=?,
                            analysis_source=?,updated_at=?,exif_json=?,captured_at=?,gps_lat=?,gps_lon=?,
                            brightness=?,contrast=?,blur_score=?,overexposed_ratio=?,underexposed_ratio=?,
                            screenshot_likelihood=?
                        WHERE id=?
                        """,
                        (
                            stat.st_size,
                            stat.st_mtime,
                            features.sha256,
                            features.perceptual_hash,
                            features.difference_hash,
                            features.width,
                            features.height,
                            features.format,
                            next_status,
                            duplicate_group,
                            next_analysis_source,
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
                            photo_id,
                        ),
                    )
                    if content_changed:
                        connection.execute("DELETE FROM photo_analysis WHERE photo_id=?", (photo_id,))
                        old_group = path_existing["duplicate_group_id"]
                        if old_group and old_group != duplicate_group:
                            remaining = connection.execute(
                                "SELECT COUNT(*) FROM photos WHERE duplicate_group_id=?", (old_group,)
                            ).fetchone()[0]
                            if remaining <= 1:
                                connection.execute(
                                    "UPDATE photos SET duplicate_group_id=NULL WHERE duplicate_group_id=?",
                                    (old_group,),
                                )
                    connection.execute("COMMIT")
                    return photo_id, inherited

                old_path_exists = bool(
                    same_content
                    and (Path(same_content["root_path"]) / str(same_content["relative_path"])).is_file()
                )
                if same_content and not old_path_exists:
                    photo_id = str(same_content["id"])
                    connection.execute(
                        "UPDATE photos SET relative_path=?,file_size=?,modified_time=?,updated_at=? WHERE id=?",
                        (relative_path, stat.st_size, stat.st_mtime, now, photo_id),
                    )
                else:
                    photo_id = str(uuid4())
                    duplicate = (
                        same_content
                        or connection.execute(
                            "SELECT id,duplicate_group_id FROM photos WHERE perceptual_hash=? LIMIT 1",
                            (features.perceptual_hash,),
                        ).fetchone()
                    )
                    duplicate_group = (duplicate["duplicate_group_id"] or str(uuid4())) if duplicate else None
                    if duplicate and not duplicate["duplicate_group_id"]:
                        connection.execute(
                            "UPDATE photos SET duplicate_group_id=? WHERE id=?",
                            (duplicate_group, duplicate["id"]),
                        )
                    connection.execute(
                        """
                        INSERT INTO photos(
                            id,library_id,relative_path,file_size,modified_time,sha256,perceptual_hash,difference_hash,
                            width,height,format,status,duplicate_group_id,analysis_source,created_at,updated_at,
                            exif_json,captured_at,gps_lat,gps_lon,brightness,contrast,blur_score,
                            overexposed_ratio,underexposed_ratio,screenshot_likelihood
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'preprocessed',?,?, ?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            photo_id,
                            library_id,
                            relative_path,
                            stat.st_size,
                            stat.st_mtime,
                            features.sha256,
                            features.perceptual_hash,
                            features.difference_hash,
                            features.width,
                            features.height,
                            features.format,
                            duplicate_group,
                            "inherited" if same_content else "direct",
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
                        ),
                    )
                connection.execute("COMMIT")
                return photo_id, inherited
            except Exception:
                connection.execute("ROLLBACK")
                raise

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

    def search(
        self,
        *,
        query: str = "",
        status: str = "",
        photo_type: str = "",
        minimum_score: float | None = None,
        duplicate_only: bool = False,
        limit: int = 60,
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
                        sensitive,reason,raw_json,analysis_source,ranking_score,scoring_version_id,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    ),
                )
                connection.execute(
                    "UPDATE photos SET status='analyzed',updated_at=? WHERE id=?", (now, photo_id)
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
