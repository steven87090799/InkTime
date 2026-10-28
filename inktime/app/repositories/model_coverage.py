"""Model completion follows exact image bytes, independently of model/prompt."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from inktime.app.domain.analysis.schema import validate_analysis_result


def remember_model_result(connection, content_sha256: str, snapshot: dict) -> bool:
    if not content_sha256 or snapshot.get("score_kind") != "semantic":
        return False
    try:
        result = validate_analysis_result(snapshot["raw_json"])
    except (KeyError, TypeError, ValueError):
        return False
    snapshot = dict(snapshot)
    location = connection.execute(
        "SELECT place_json FROM photo_locations WHERE photo_id=? AND content_sha256=?",
        (snapshot.get("photo_id"), content_sha256),
    ).fetchone()
    previous = connection.execute(
        "SELECT analysis_snapshot_json FROM model_analysis_results WHERE content_sha256=?", (content_sha256,),
    ).fetchone()
    if location:
        place = json.loads(location[0])
        snapshot["landmark_evidence"] = {key: place.get(key) for key in ("landmark", "landmark_confidence", "landmark_source")}
    elif previous:
        evidence = json.loads(previous[0]).get("landmark_evidence")
        if evidence is not None:
            snapshot["landmark_evidence"] = evidence
    # Landmark absence and a model's exclusion verdict are both valid answers.
    connection.execute(
        "INSERT INTO model_analysis_results(content_sha256,analysis_snapshot_json,schema_version,updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(content_sha256) DO UPDATE SET "
        "analysis_snapshot_json=excluded.analysis_snapshot_json,schema_version=excluded.schema_version,"
        "updated_at=excluded.updated_at WHERE model_analysis_results.analysis_snapshot_json<>excluded.analysis_snapshot_json "
        "AND COALESCE(json_extract(model_analysis_results.analysis_snapshot_json,'$.created_at'),'')"
        "<=COALESCE(json_extract(excluded.analysis_snapshot_json,'$.created_at'),'')",
        (content_sha256, json.dumps(snapshot, ensure_ascii=False), result["schema_version"],
         datetime.now(timezone.utc).isoformat()),
    )
    return True


def reconcile_model_results(connection) -> int:
    """Bounded-memory audit of live and preserved results; never calls a model."""
    count = 0
    rows = connection.execute(
        "SELECT a.*,COALESCE(a.content_sha256,p.sha256) AS result_sha "
        "FROM photo_analysis a JOIN photos p ON p.id=a.photo_id "
        "WHERE a.score_kind='semantic' ORDER BY a.id"
    )
    for row in rows:
        count += int(remember_model_result(connection, str(row["result_sha"] or ""), dict(row)))
    for row in connection.execute(
        "SELECT content_sha256,analyses_json FROM photo_content_history ORDER BY created_at,id"
    ):
        try:
            snapshots = json.loads(row["analyses_json"])
            if not isinstance(snapshots, list):
                continue
            for snapshot in snapshots:
                if isinstance(snapshot, dict):
                    count += int(remember_model_result(connection, str(row["content_sha256"]), snapshot))
        except (TypeError, ValueError):
            continue
    for row in connection.execute(
        "SELECT content_sha256,analysis_snapshot_json FROM seasonal_analysis_tasks "
        "WHERE analysis_snapshot_json IS NOT NULL ORDER BY id"
    ):
        try:
            snapshot = json.loads(row["analysis_snapshot_json"])
            if isinstance(snapshot, dict):
                count += int(remember_model_result(connection, str(row["content_sha256"]), snapshot))
        except (TypeError, ValueError):
            continue
    return count
