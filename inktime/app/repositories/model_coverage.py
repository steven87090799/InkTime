"""Model completion follows exact image bytes, independently of model/prompt."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from inktime.app.domain.analysis.schema import validate_analysis_result
from inktime.app.repositories.analysis_history import historical_model_sql


def preserved_outputs_sql(content_sha: str) -> str:
    """Older model-only sources that can predate the current coverage archive."""
    return f"""
        SELECT json_object('schema_version',old.schema_version,'raw_json',old.raw_json,'side_caption',old.side_caption,
                           'provider',old.provider,'model',old.model,'prompt_version',old.prompt_version,'schema_kind',old.schema_kind,
                           'analysis_fingerprint',old.analysis_fingerprint,'analysis_spec_json',old.analysis_spec_json,
                           'vision_request_fingerprint',old.vision_request_fingerprint,'vision_input_spec_json',old.vision_input_spec_json,
                           'scoring_version_id',old.scoring_version_id,'created_at',old.created_at) AS snapshot
        FROM photo_analysis old NOT INDEXED WHERE old.id IN (
            SELECT exact.id FROM photo_analysis exact WHERE exact.content_sha256={content_sha}
            UNION ALL
            SELECT legacy.id FROM photos original CROSS JOIN photo_analysis legacy INDEXED BY idx_photo_analysis_photo_id
              ON legacy.photo_id=original.id WHERE original.sha256={content_sha} AND legacy.content_sha256 IS NULL)
          AND ((old.score_kind='semantic' AND old.schema_version IN (4,5,6)) OR {historical_model_sql('old')})
        UNION ALL
        SELECT record.value AS snapshot FROM (
          SELECT item.value FROM photo_content_history h,
            json_each(CASE WHEN json_valid(h.analyses_json) THEN h.analyses_json ELSE '[]' END) item
          WHERE h.content_sha256={content_sha} AND item.type='object'
          UNION ALL
          SELECT season.analysis_snapshot_json FROM seasonal_analysis_tasks season WHERE season.content_sha256={content_sha}
            AND json_valid(season.analysis_snapshot_json)
        ) record
        WHERE COALESCE(json_extract(record.value,'$.content_sha256'),{content_sha})={content_sha}
          AND ((json_extract(record.value,'$.score_kind')='semantic'
                AND json_extract(record.value,'$.schema_version') IN (4,5,6))
               OR {historical_model_sql('record', snapshot=True)})
        UNION ALL
        SELECT json_object('schema_version',c.schema_version,'raw_json',c.result_json,'provider',c.provider,
                           'model',c.model_name,'prompt_version',c.prompt_version,'schema_kind',c.schema_kind,
                           'vision_request_fingerprint',c.vision_request_fingerprint,
                           'vision_input_spec_json',c.vision_input_spec_json,'created_at',c.created_at) AS snapshot
        FROM ai_analysis_cache c WHERE c.content_sha256={content_sha} AND c.schema_version BETWEEN 1 AND 6
          AND lower(trim(c.provider)) NOT IN ('','inherited','local','local-prefilter','local-quality-v3','virtual-display-local')
    """  # noqa: S608 -- only fixed repository column names or a bound parameter.


def saved_model_result_sql(content_sha: str) -> str:
    """Match by exact bytes, including live rows from before the archive."""
    return f"""(
        EXISTS(SELECT 1 FROM model_analysis_results saved WHERE saved.content_sha256={content_sha})
        OR EXISTS({preserved_outputs_sql(content_sha)})
    )"""  # noqa: S608 -- fixed column expressions supplied by repository callers.


def has_saved_model_result(connection, content_sha: str) -> bool:
    return bool(connection.execute(
        "SELECT " + saved_model_result_sql(":sha"), {"sha": content_sha},  # noqa: S608 -- fixed SQL, bound hash.
    ).fetchone()[0])


def saved_model_evidence_sql(content_sha: str) -> str:
    return f"""({saved_model_result_sql(content_sha)} OR EXISTS(
        SELECT 1 FROM billable_operations o
        LEFT JOIN model_request_evidence e ON e.operation_id=o.id
        WHERE o.content_sha256={content_sha} AND o.state IN ('started','response','completed')
          AND COALESCE(o.response_json,e.response_json) IS NOT NULL
    ))"""  # noqa: S608 -- fixed column expressions supplied by repository callers.


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
    def optional_object(raw):
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    place = optional_object(location[0]) if location else {}
    if place.get("landmark_source") == "vision":
        snapshot["landmark_evidence"] = {key: place.get(key) for key in ("landmark", "landmark_confidence", "landmark_source")}
    elif previous:
        evidence = optional_object(previous[0]).get("landmark_evidence")
        if evidence is not None:
            snapshot["landmark_evidence"] = evidence
    # Landmark absence and a model's exclusion verdict are both valid answers.
    connection.execute(
        "INSERT INTO model_analysis_results(content_sha256,analysis_snapshot_json,schema_version,updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(content_sha256) DO UPDATE SET "
        "analysis_snapshot_json=excluded.analysis_snapshot_json,schema_version=excluded.schema_version,"
        "updated_at=excluded.updated_at WHERE model_analysis_results.analysis_snapshot_json<>excluded.analysis_snapshot_json "
        "AND CASE WHEN json_valid(model_analysis_results.analysis_snapshot_json) "
        "THEN COALESCE(json_extract(model_analysis_results.analysis_snapshot_json,'$.created_at'),'') ELSE '' END"
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
