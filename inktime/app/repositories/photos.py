from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from inktime.app.db import Database
from inktime.app.domain.photos.preprocessing import LocalPhotoFeatures


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

    def upsert_preprocessed(self, library_id: str, relative_path: str, source: Path, features: LocalPhotoFeatures) -> tuple[str, bool]:
        now = datetime.now(timezone.utc).isoformat()
        stat = source.stat()
        values = features.as_dict()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT p.id,p.relative_path,p.duplicate_group_id,l.root_path
                    FROM photos p JOIN libraries l ON l.id=p.library_id
                    WHERE p.library_id=? AND p.sha256=? ORDER BY p.created_at LIMIT 1
                    """,
                    (library_id, features.sha256),
                ).fetchone()
                inherited = existing is not None and existing["relative_path"] != relative_path
                old_path_exists = bool(
                    existing
                    and (Path(existing["root_path"]) / str(existing["relative_path"])).is_file()
                )
                if existing and (existing["relative_path"] == relative_path or not old_path_exists):
                    photo_id = str(existing["id"])
                    connection.execute(
                        "UPDATE photos SET relative_path=?,file_size=?,modified_time=?,updated_at=? WHERE id=?",
                        (relative_path, stat.st_size, stat.st_mtime, now, photo_id),
                    )
                else:
                    photo_id = str(uuid4())
                    duplicate = existing or connection.execute(
                        "SELECT id,duplicate_group_id FROM photos WHERE perceptual_hash=? LIMIT 1",
                        (features.perceptual_hash,),
                    ).fetchone()
                    duplicate_group = (duplicate["duplicate_group_id"] or str(uuid4())) if duplicate else None
                    if duplicate and not duplicate["duplicate_group_id"]:
                        connection.execute("UPDATE photos SET duplicate_group_id=? WHERE id=?", (duplicate_group, duplicate["id"]))
                    connection.execute(
                        """
                        INSERT INTO photos(
                            id,library_id,relative_path,file_size,modified_time,sha256,perceptual_hash,difference_hash,
                            width,height,format,status,duplicate_group_id,analysis_source,created_at,updated_at,
                            exif_json,captured_at,gps_lat,gps_lon,brightness,contrast,blur_score,
                            overexposed_ratio,underexposed_ratio,screenshot_likelihood
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'preprocessed',?,?, ?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (photo_id, library_id, relative_path, stat.st_size, stat.st_mtime, features.sha256,
                         features.perceptual_hash, features.difference_hash, features.width, features.height,
                         features.format, duplicate_group, "inherited" if existing else "direct", now, now, values["exif_json"], values["captured_at"],
                         values["gps_lat"], values["gps_lon"], values["brightness"], values["contrast"],
                         values["blur_score"], values["overexposed_ratio"], values["underexposed_ratio"],
                         values["screenshot_likelihood"]),
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
            "schema_version": row["schema_version"], "caption": row["caption"],
            "types": json.loads(row["types_json"]), "memory_score": row["memory_score"],
            "beauty_score": row["beauty_score"], "technical_quality_score": row["technical_quality_score"],
            "emotion_score": row["emotion_score"], "side_caption": row["side_caption"],
            "should_keep": bool(row["should_keep"]), "sensitive": bool(row["sensitive"]),
            "reason": row["reason"],
        }
        self.save_analysis(photo_id, job_id, "inherited", row["provider"] or "inherited", row["model"] or "inherited", result, row["raw_json"], "inherited")
        return result

    def get_with_path(self, photo_id: str):
        with self.database.session() as connection:
            return connection.execute(
                """
                SELECT p.*, l.root_path FROM photos p JOIN libraries l ON l.id=p.library_id WHERE p.id=?
                """,
                (photo_id,),
            ).fetchone()

    def save_analysis(self, photo_id: str, job_id: str | None, stage: str, provider: str, model: str, result: dict, raw_json: str, analysis_source: str = "direct") -> None:
        import json
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO photo_analysis(photo_id,job_id,schema_version,stage,provider,model,caption,types_json,
                        memory_score,beauty_score,technical_quality_score,emotion_score,side_caption,should_keep,
                        sensitive,reason,raw_json,analysis_source,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (photo_id, job_id, result["schema_version"], stage, provider, model, result["caption"],
                     json.dumps(result["types"], ensure_ascii=False), result["memory_score"], result["beauty_score"],
                     result["technical_quality_score"], result["emotion_score"], result["side_caption"],
                     int(result["should_keep"]), int(result["sensitive"]), result["reason"], raw_json,
                     analysis_source, now),
                )
                connection.execute("UPDATE photos SET status='analyzed',updated_at=? WHERE id=?", (now, photo_id))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
