from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from inktime.app.core.ai_trace import sanitized_json_text, sanitized_response_text
from inktime.app.db import Database


TRACE_LIST_LIMIT = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


class AITraceRepository:
    """Small, read-optimized persistence for synchronous model observability."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def start_trace(
        self,
        *,
        trace_id: str,
        job_id: str | None,
        photo_id: str,
        provider: str,
        model: str,
        stage: str,
        prompt_version: str,
        analysis_fingerprint: str | None,
        started_at: str,
    ) -> None:
        now = _now()
        with self.database.session() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO model_call_traces(
                    trace_id,job_id,photo_id,provider,model,stage,status,prompt_version,
                    analysis_fingerprint,started_at,created_at
                ) VALUES (?,?,?,?,?,?,'RUNNING',?,?,?,?)
                """,
                (
                    trace_id,
                    job_id,
                    photo_id,
                    provider,
                    model,
                    stage,
                    prompt_version,
                    analysis_fingerprint,
                    started_at,
                    now,
                ),
            )
            if cursor.rowcount:
                connection.execute(
                    "INSERT INTO model_call_trace_events(trace_id,event_type,details_json,created_at) "
                    "VALUES (?,'PHOTO_SELECTED','{}',?)",
                    (trace_id, started_at),
                )
            else:
                connection.execute(
                    """
                    UPDATE model_call_traces
                    SET provider=?,model=?,status='RUNNING',completed_at=NULL,
                        error_code=NULL,error_message=NULL
                    WHERE trace_id=?
                    """,
                    (provider, model, trace_id),
                )

    def start_attempt(
        self,
        *,
        trace_id: str,
        provider: str,
        model: str,
        started_at: str,
        retry_reason: str | None = None,
        retry_delay_ms: int | None = None,
    ) -> int:
        with self.database.transaction(operation="ai_trace_start_attempt") as connection:
            number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(attempt_number),0)+1 FROM model_call_attempts WHERE trace_id=?",
                    (trace_id,),
                ).fetchone()[0]
            )
            cursor = connection.execute(
                """
                INSERT INTO model_call_attempts(
                    trace_id,attempt_number,provider,model,status,started_at,retry_reason,retry_delay_ms
                ) VALUES (?,?,?,?,'RUNNING',?,?,?)
                """,
                (
                    trace_id,
                    number,
                    provider,
                    model,
                    started_at,
                    retry_reason or ("provider_failover" if number > 1 else None),
                    retry_delay_ms,
                ),
            )
            attempt_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO model_call_trace_events(trace_id,attempt_id,event_type,details_json,created_at) "
                "VALUES (?,?,'PREPROCESS_STARTED','{}',?)",
                (trace_id, attempt_id, started_at),
            )
            connection.execute(
                "INSERT INTO model_call_trace_events(trace_id,attempt_id,event_type,details_json,created_at) "
                "VALUES (?,?,'IMAGE_READY','{}',?)",
                (trace_id, attempt_id, started_at),
            )
            connection.execute(
                "INSERT INTO model_call_trace_events(trace_id,attempt_id,event_type,details_json,created_at) "
                "VALUES (?,?,'PROVIDER_REQUEST_STARTED',?,?)",
                (
                    trace_id,
                    attempt_id,
                    sanitized_json_text({"provider": provider, "model": model, "attempt": number}),
                    started_at,
                ),
            )
        return attempt_id

    def add_event(
        self,
        trace_id: str,
        event_type: str,
        *,
        attempt_id: int | None = None,
        details: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> None:
        with self.database.session() as connection:
            connection.execute(
                """
                INSERT INTO model_call_trace_events(trace_id,attempt_id,event_type,details_json,created_at)
                VALUES (?,?,?,?,?)
                """,
                (
                    trace_id,
                    attempt_id,
                    event_type[:80],
                    sanitized_json_text(details or {}),
                    created_at or _now(),
                ),
            )

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        result: str,
        request_json: Any = None,
        response_raw: Any = None,
        response_parsed: Any = None,
        request_built_at: str | None = None,
        response_received_at: str | None = None,
        completed_at: str | None = None,
        endpoint: str | None = None,
        api_mode: str | None = None,
        http_status: int | None = None,
        latency_ms: int | None = None,
        provider_request_id: str | None = None,
        api_usage_id: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        retry_delay_ms: int | None = None,
    ) -> None:
        completed = completed_at or _now()
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT trace_id FROM model_call_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                return
            trace_id = str(row["trace_id"])
            connection.execute(
                """
                UPDATE model_call_attempts SET
                    endpoint=?,api_mode=?,status=?,result=?,request_json_sanitized=?,response_raw=?,
                    response_parsed_json=?,request_built_at=?,response_received_at=?,completed_at=?,
                    http_status=?,latency_ms=?,provider_request_id=?,api_usage_id=?,error_code=?,
                    error_message=?,retry_delay_ms=COALESCE(?,retry_delay_ms)
                WHERE id=?
                """,
                (
                    endpoint,
                    api_mode,
                    status,
                    result[:80],
                    sanitized_json_text(request_json) if request_json is not None else None,
                    sanitized_response_text(response_raw) if response_raw is not None else None,
                    sanitized_json_text(response_parsed) if response_parsed is not None else None,
                    request_built_at,
                    response_received_at,
                    completed,
                    http_status,
                    max(0, int(latency_ms)) if latency_ms is not None else None,
                    sanitized_response_text(provider_request_id)[:255] if provider_request_id else None,
                    api_usage_id,
                    error_code[:120] if error_code else None,
                    sanitized_response_text(error_message)[:1000] if error_message else None,
                    max(0, int(retry_delay_ms)) if retry_delay_ms is not None else None,
                    attempt_id,
                ),
            )
            if request_built_at:
                connection.execute(
                    "INSERT INTO model_call_trace_events(trace_id,attempt_id,event_type,details_json,created_at) "
                    "VALUES (?,?,'REQUEST_BUILT','{}',?)",
                    (trace_id, attempt_id, request_built_at),
                )
            if response_received_at:
                connection.execute(
                    "INSERT INTO model_call_trace_events(trace_id,attempt_id,event_type,details_json,created_at) "
                    "VALUES (?,?,'PROVIDER_RESPONSE_RECEIVED',?,?)",
                    (
                        trace_id,
                        attempt_id,
                        sanitized_json_text({"http_status": http_status, "result": result}),
                        response_received_at,
                    ),
                )

    def mark_trace(
        self,
        trace_id: str,
        *,
        status: str,
        response_received_at: str | None = None,
        completed_at: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        completed = completed_at or (_now() if status != "RUNNING" else None)
        with self.database.session() as connection:
            connection.execute(
                """
                UPDATE model_call_traces SET status=?,response_received_at=COALESCE(?,response_received_at),
                    completed_at=?,error_code=?,error_message=? WHERE trace_id=?
                """,
                (
                    status,
                    response_received_at,
                    completed,
                    error_code[:120] if error_code else None,
                    sanitized_response_text(error_message)[:1000] if error_message else None,
                    trace_id,
                ),
            )

    def persist_final_result(self, trace_id: str, result: dict[str, Any]) -> None:
        completed = _now()
        with self.database.session() as connection:
            connection.execute(
                """
                UPDATE model_call_traces
                SET final_result_json=?,status='SUCCESS',completed_at=?,error_code=NULL,error_message=NULL
                WHERE trace_id=?
                """,
                (sanitized_json_text(result), completed, trace_id),
            )
            connection.execute(
                "INSERT INTO model_call_trace_events(trace_id,event_type,details_json,created_at) "
                "VALUES (?,'RESULT_PERSISTED','{}',?)",
                (trace_id, completed),
            )
            connection.execute(
                "INSERT INTO model_call_trace_events(trace_id,event_type,details_json,created_at) "
                "VALUES (?,'COMPLETE','{}',?)",
                (trace_id, completed),
            )

    def list(
        self,
        *,
        filters: dict[str, str],
        limit: int = 50,
        before: int | None = None,
        after: int | None = None,
    ) -> list[dict[str, Any]]:
        size = max(1, min(int(limit), TRACE_LIST_LIMIT))
        clauses: list[str] = []
        values: list[Any] = []
        columns = {
            "status": "t.status",
            "provider": "t.provider",
            "model": "t.model",
            "job_id": "t.job_id",
            "photo_id": "t.photo_id",
            "stage": "t.stage",
        }
        for key, column in columns.items():
            value = str(filters.get(key) or "").strip()
            if value:
                clauses.append(f"{column}=?")
                values.append(value)
        if before is not None:
            clauses.append("t.id<?")
            values.append(max(1, int(before)))
        if after is not None:
            clauses.append("t.id>?")
            values.append(max(0, int(after)))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.session() as connection:
            rows = connection.execute(
                f"""
                WITH candidates AS MATERIALIZED (
                    SELECT t.* FROM model_call_traces t
                    {where}
                    ORDER BY t.id DESC LIMIT ?
                )
                SELECT t.id,t.trace_id,t.job_id,t.photo_id,t.provider,t.model,t.stage,t.status,
                       t.started_at,t.response_received_at,t.completed_at,t.error_code,
                       p.relative_path,p.width,p.height,p.format,
                       COUNT(a.id) AS attempt_count,
                       (SELECT ax.http_status FROM model_call_attempts ax
                           WHERE ax.trace_id=t.trace_id ORDER BY ax.attempt_number DESC LIMIT 1)
                           AS http_status,
                       COALESCE(SUM(u.input_tokens),0) AS input_tokens,
                       COALESCE(SUM(u.output_tokens),0) AS output_tokens,
                       COALESCE(SUM(u.cached_tokens),0) AS cached_tokens,
                       COALESCE(SUM(u.reasoning_tokens),0) AS reasoning_tokens,
                       COALESCE(SUM(CASE WHEN u.cost_source<>'unknown'
                           THEN COALESCE(u.actual_cost,u.estimated_cost) ELSE 0 END),0) AS cost,
                       SUM(CASE WHEN u.id IS NOT NULL AND u.cost_source='unknown' THEN 1 ELSE 0 END)
                           AS unknown_cost_count,
                       CASE WHEN SUM(CASE WHEN u.cost_source='estimated' THEN 1 ELSE 0 END)>0
                           THEN 'estimated'
                           WHEN SUM(CASE WHEN u.cost_source='provider_reported' THEN 1 ELSE 0 END)>0
                           THEN 'provider_reported' ELSE 'unknown' END AS cost_source,
                       COALESCE(SUM(COALESCE(u.latency_ms,a.latency_ms)),0) AS latency_ms
                FROM candidates t
                JOIN photos p ON p.id=t.photo_id
                LEFT JOIN model_call_attempts a ON a.trace_id=t.trace_id
                LEFT JOIN api_usage u ON u.id=a.api_usage_id
                GROUP BY t.id
                ORDER BY t.id DESC
                """,  # noqa: S608 -- columns and clauses are fixed allowlisted fragments
                (*values, size),
            ).fetchall()
        return [dict(row) for row in rows]

    def detail(self, trace_id: str, *, include_payloads: bool) -> dict[str, Any] | None:
        with self.database.session() as connection:
            trace = connection.execute(
                """
                SELECT t.*,p.relative_path,p.width,p.height,p.format,p.sha256,p.captured_at,
                       p.camera_make,p.camera_model,p.lens_model,p.exif_orientation_original,
                       j.name AS job_name
                FROM model_call_traces t
                JOIN photos p ON p.id=t.photo_id
                LEFT JOIN jobs j ON j.id=t.job_id
                WHERE t.trace_id=?
                """,
                (trace_id,),
            ).fetchone()
            if trace is None:
                return None
            attempts = connection.execute(
                """
                SELECT a.*,u.input_tokens,u.output_tokens,u.cached_tokens,u.reasoning_tokens,
                       u.cache_write_tokens,u.estimated_cost,u.actual_cost,u.cost_source,
                       u.latency_ms AS usage_latency_ms
                FROM model_call_attempts a
                LEFT JOIN api_usage u ON u.id=a.api_usage_id
                WHERE a.trace_id=? ORDER BY a.attempt_number
                """,
                (trace_id,),
            ).fetchall()
            events = connection.execute(
                "SELECT id,attempt_id,event_type,details_json,created_at "
                "FROM model_call_trace_events WHERE trace_id=? ORDER BY created_at,id",
                (trace_id,),
            ).fetchall()
        result = dict(trace)
        result["final_result"] = _json(result.pop("final_result_json", None), None)
        result["exif"] = {
            key: result.get(key)
            for key in ("camera_make", "camera_model", "lens_model", "exif_orientation_original")
            if result.get(key) not in (None, "")
        }
        result["mime_type"] = {
            "JPEG": "image/jpeg",
            "JPG": "image/jpeg",
            "PNG": "image/png",
            "WEBP": "image/webp",
            "HEIC": "image/heic",
        }.get(str(result.get("format") or "").upper())
        result["attempts"] = []
        for stored in attempts:
            attempt = dict(stored)
            attempt["latency_ms"] = attempt.pop("usage_latency_ms", None) or attempt.get("latency_ms")
            request_payload = _json(attempt.pop("request_json_sanitized", None), None)
            parsed = _json(attempt.pop("response_parsed_json", None), None)
            raw = attempt.pop("response_raw", None)
            attempt["response_parsed"] = parsed
            if include_payloads:
                attempt["request"] = request_payload
                attempt["response_raw"] = raw
                system_prompts: list[str] = []
                user_prompts: list[str] = []
                if isinstance(request_payload, dict):
                    for message in request_payload.get("messages", []):
                        if not isinstance(message, dict):
                            continue
                        content = message.get("content")
                        texts = (
                            [str(item.get("text")) for item in content if isinstance(item, dict) and item.get("text")]
                            if isinstance(content, list)
                            else [str(content)] if content is not None else []
                        )
                        if message.get("role") == "system":
                            system_prompts.extend(texts)
                        elif message.get("role") == "user":
                            user_prompts.extend(texts)
                    attempt["response_format"] = request_payload.get("response_format")
                attempt["system_prompt"] = "\n\n".join(system_prompts) or None
                attempt["user_prompt"] = "\n\n".join(user_prompts) or None
            else:
                attempt["request"] = None
                attempt["response_raw"] = None
                attempt["response_format"] = None
                attempt["system_prompt"] = None
                attempt["user_prompt"] = None
            attempt["cost"] = (
                attempt.get("actual_cost")
                if attempt.get("cost_source") == "provider_reported"
                else attempt.get("estimated_cost")
                if attempt.get("cost_source") == "estimated"
                else None
            )
            result["attempts"].append(attempt)
        result["events"] = [
            {**dict(row), "details": _json(row["details_json"], {})} for row in events
        ]
        result["raw_payloads_visible"] = bool(include_payloads)
        return result
