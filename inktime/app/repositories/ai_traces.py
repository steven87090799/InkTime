from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
from uuid import uuid4

from inktime.app.core.ai_trace_payloads import bounded_json_text, bounded_text, sanitize_trace_value
from inktime.app.db import Database
from inktime.app.providers.base import ProviderCallTrace


RUN_STATUSES = {"RUNNING", "SUCCESS", "FAILED", "TIMEOUT", "AMBIGUOUS"}
ATTEMPT_STATUSES = {"RUNNING", "SUCCESS", "FAILED", "TIMEOUT", "AMBIGUOUS", "VALIDATION_FAILED"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AITraceRepository:
    """Bounded, observational persistence; callers own the fail-open boundary."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def start_run(
        self,
        *,
        job_id: str | None,
        photo_id: str,
        stage: str,
        prompt_version: str,
        analysis_fingerprint: str | None,
        vision_request_fingerprint: str,
    ) -> str:
        trace_id = str(uuid4())
        now = _now()
        with self.database.session() as connection:
            connection.execute(
                """
                INSERT INTO ai_trace_runs(
                    trace_id,job_id,photo_id,stage,prompt_version,analysis_fingerprint,
                    vision_request_fingerprint,status,started_at,created_at
                ) VALUES (?,?,?,?,?,?,?,'RUNNING',?,?)
                """,
                (
                    trace_id,
                    job_id,
                    photo_id,
                    str(stage)[:80],
                    str(prompt_version)[:160],
                    str(analysis_fingerprint)[:160] if analysis_fingerprint else None,
                    str(vision_request_fingerprint)[:160],
                    now,
                    now,
                ),
            )
        return trace_id

    def start_attempt(
        self,
        trace_id: str,
        *,
        attempt_kind: str,
        provider: str,
        provider_id: str | None,
        requested_model: str,
    ) -> int:
        now = _now()
        with self.database.transaction(operation="ai_trace_attempt_start") as connection:
            number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(attempt_number),0)+1 FROM ai_trace_attempts WHERE trace_id=?",
                    (trace_id,),
                ).fetchone()[0]
            )
            cursor = connection.execute(
                """
                INSERT INTO ai_trace_attempts(
                    trace_id,attempt_number,attempt_kind,provider,provider_id,
                    requested_model,status,created_at
                ) VALUES (?,?,?,?,?,?,'RUNNING',?)
                """,
                (
                    trace_id,
                    number,
                    attempt_kind if attempt_kind in {"vision", "json_repair"} else "vision",
                    str(provider)[:128],
                    str(provider_id)[:128] if provider_id else None,
                    str(requested_model)[:160],
                    now,
                ),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("AI-TRACE-002 attempt id unavailable")
        return int(cursor.lastrowid)

    def update_attempt_from_call(
        self,
        attempt_id: int,
        call_trace: ProviderCallTrace | None,
        *,
        status: str | None = None,
        served_model: str | None = None,
        api_usage_id: int | None = None,
        response_parsed: Any | None = None,
        parse_started_at: str | None = None,
        parsed_at: str | None = None,
        retry_reason: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        selected_status = status if status in ATTEMPT_STATUSES else None
        trace = call_trace or ProviderCallTrace()
        request_json = (
            bounded_json_text(trace.request_json_sanitized, maximum_bytes=65_536)
            if trace.request_json_sanitized is not None
            else None
        )
        parsed_json = (
            bounded_json_text(response_parsed, maximum_bytes=65_536) if response_parsed is not None else None
        )
        with self.database.session() as connection:
            connection.execute(
                """
                UPDATE ai_trace_attempts SET
                    endpoint=COALESCE(?,endpoint),api_mode=COALESCE(?,api_mode),
                    served_model=COALESCE(?,served_model),status=COALESCE(?,status),
                    request_json_sanitized=COALESCE(?,request_json_sanitized),
                    response_raw_sanitized=COALESCE(?,response_raw_sanitized),
                    response_parsed_json=COALESCE(?,response_parsed_json),
                    request_built_at=COALESCE(?,request_built_at),
                    request_started_at=COALESCE(?,request_started_at),
                    response_received_at=COALESCE(?,response_received_at),
                    parse_started_at=COALESCE(?,parse_started_at),parsed_at=COALESCE(?,parsed_at),
                    completed_at=COALESCE(?,completed_at),http_status=COALESCE(?,http_status),
                    provider_request_id=COALESCE(?,provider_request_id),api_usage_id=COALESCE(?,api_usage_id),
                    latency_ms=COALESCE(?,latency_ms),retry_reason=COALESCE(?,retry_reason),
                    error_code=COALESCE(?,error_code),error_message=COALESCE(?,error_message)
                WHERE id=?
                """,
                (
                    bounded_text(trace.endpoint, maximum_bytes=1024) if trace.endpoint else None,
                    bounded_text(trace.api_mode, maximum_bytes=80) if trace.api_mode else None,
                    str(served_model or trace.served_model)[:160]
                    if served_model or trace.served_model
                    else None,
                    selected_status,
                    request_json,
                    bounded_text(trace.response_raw_sanitized, maximum_bytes=65_536)
                    if trace.response_raw_sanitized is not None
                    else None,
                    parsed_json,
                    trace.request_built_at,
                    trace.request_started_at,
                    trace.response_received_at,
                    parse_started_at,
                    parsed_at,
                    completed_at or trace.completed_at,
                    trace.http_status,
                    str(trace.provider_request_id)[:255] if trace.provider_request_id else None,
                    api_usage_id,
                    trace.latency_ms,
                    str(retry_reason)[:500] if retry_reason else None,
                    str(error_code)[:128] if error_code else None,
                    bounded_text(error_message, maximum_bytes=1000) if error_message else None,
                    int(attempt_id),
                ),
            )

    def complete_run(
        self,
        trace_id: str,
        *,
        status: str,
        final_result: Any | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        response_received_at: str | None = None,
        scoring_completed_at: str | None = None,
        result_persisted_at: str | None = None,
    ) -> None:
        selected = status if status in RUN_STATUSES else "FAILED"
        completed_at = _now()
        with self.database.session() as connection:
            connection.execute(
                """
                UPDATE ai_trace_runs SET status=?,response_received_at=COALESCE(?,response_received_at),
                    scoring_completed_at=COALESCE(?,scoring_completed_at),
                    result_persisted_at=COALESCE(?,result_persisted_at),completed_at=?,
                    final_result_json=COALESCE(?,final_result_json),error_code=?,error_message=?
                WHERE trace_id=?
                """,
                (
                    selected,
                    response_received_at,
                    scoring_completed_at,
                    result_persisted_at,
                    completed_at,
                    bounded_json_text(final_result, maximum_bytes=65_536)
                    if final_result is not None
                    else None,
                    str(error_code)[:128] if error_code else None,
                    bounded_text(error_message, maximum_bytes=1000) if error_message else None,
                    trace_id,
                ),
            )

    def update_run_progress(
        self,
        trace_id: str,
        *,
        response_received_at: str | None = None,
        scoring_completed_at: str | None = None,
        result_persisted_at: str | None = None,
    ) -> None:
        with self.database.session() as connection:
            connection.execute(
                """
                UPDATE ai_trace_runs SET response_received_at=COALESCE(?,response_received_at),
                    scoring_completed_at=COALESCE(?,scoring_completed_at),
                    result_persisted_at=COALESCE(?,result_persisted_at)
                WHERE trace_id=?
                """,
                (response_received_at, scoring_completed_at, result_persisted_at, trace_id),
            )

    @staticmethod
    def _filters(filters: dict[str, str]) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        mapping = {
            "status": "r.status",
            "job_id": "r.job_id",
            "photo_id": "r.photo_id",
            "stage": "r.stage",
            "trace_id": "r.trace_id",
        }
        for name, column in mapping.items():
            value = str(filters.get(name) or "").strip()
            if value:
                clauses.append(f"{column}=?")
                parameters.append(value[:160])
        provider = str(filters.get("provider") or "").strip()
        if provider:
            clauses.append(
                "EXISTS (SELECT 1 FROM ai_trace_attempts filtered_provider "
                "WHERE filtered_provider.trace_id=r.trace_id "
                "AND filtered_provider.provider=?)"
            )
            parameters.append(provider[:160])
        model = str(filters.get("model") or "").strip()
        if model:
            clauses.append(
                "EXISTS (SELECT 1 FROM ai_trace_attempts filtered_model "
                "WHERE filtered_model.trace_id=r.trace_id "
                "AND (filtered_model.requested_model=? OR filtered_model.served_model=?))"
            )
            parameters.extend((model[:160], model[:160]))
        return clauses, parameters

    def list_runs(
        self,
        *,
        filters: dict[str, str],
        limit: int = 50,
        before_id: int | None = None,
    ) -> dict[str, Any]:
        size = max(1, min(int(limit), 100))
        clauses, parameters = self._filters(filters)
        if before_id is not None:
            clauses.append("r.id<?")
            parameters.append(max(1, int(before_id)))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(size + 1)
        with self.database.session() as connection:
            rows = connection.execute(
                f"""
                SELECT r.id,r.trace_id,r.job_id,r.photo_id,r.stage,r.status,r.started_at,r.completed_at,
                       r.error_code,r.error_message,p.relative_path,p.width,p.height,
                       COUNT(a.id) attempt_count,
                       (SELECT first_attempt.provider FROM ai_trace_attempts first_attempt
                        WHERE first_attempt.trace_id=r.trace_id
                        ORDER BY first_attempt.attempt_number,first_attempt.id LIMIT 1) provider,
                       (SELECT first_attempt.requested_model FROM ai_trace_attempts first_attempt
                        WHERE first_attempt.trace_id=r.trace_id
                        ORDER BY first_attempt.attempt_number,first_attempt.id LIMIT 1) requested_model,
                       (SELECT latest_served.served_model FROM ai_trace_attempts latest_served
                        WHERE latest_served.trace_id=r.trace_id
                          AND latest_served.served_model IS NOT NULL
                        ORDER BY latest_served.attempt_number DESC,latest_served.id DESC LIMIT 1) served_model,
                       (SELECT latest_attempt.http_status FROM ai_trace_attempts latest_attempt
                        WHERE latest_attempt.trace_id=r.trace_id
                        ORDER BY latest_attempt.attempt_number DESC,latest_attempt.id DESC LIMIT 1) latest_http_status,
                       SUM(COALESCE(a.latency_ms,0)) latency_ms,
                       SUM(COALESCE(u.input_tokens,0)) input_tokens,
                       SUM(COALESCE(u.output_tokens,0)) output_tokens,
                       SUM(COALESCE(u.cached_tokens,0)) cached_tokens,
                       SUM(CASE WHEN u.cost_source='provider_reported' THEN u.actual_cost
                                WHEN u.cost_source='estimated' THEN u.estimated_cost ELSE NULL END) cost,
                       CASE WHEN COUNT(u.id)=0 THEN 'unknown'
                            WHEN SUM(CASE WHEN u.cost_source='unknown' THEN 1 ELSE 0 END)>0 THEN 'unknown'
                            WHEN SUM(CASE WHEN u.cost_source='estimated' THEN 1 ELSE 0 END)>0 THEN 'estimated'
                            ELSE 'provider_reported' END cost_source
                FROM ai_trace_runs r
                LEFT JOIN ai_trace_attempts a ON a.trace_id=r.trace_id
                LEFT JOIN api_usage u ON u.id=a.api_usage_id
                LEFT JOIN photos p ON p.id=r.photo_id
                {where}
                GROUP BY r.id
                ORDER BY r.id DESC
                LIMIT ?
                """,  # noqa: S608 -- clauses come from a fixed mapping above
                tuple(parameters),
            ).fetchall()
        has_more = len(rows) > size
        page = [dict(row) for row in rows[:size]]
        return {
            "traces": [sanitize_trace_value(row) for row in page],
            "next_cursor": page[-1]["id"] if has_more and page else None,
        }

    def detail(self, trace_id: str, *, include_sensitive: bool) -> dict[str, Any] | None:
        with self.database.session() as connection:
            run = connection.execute(
                """
                SELECT r.*,p.relative_path,p.width,p.height,p.format,p.sha256,
                       p.captured_at,p.exif_json
                FROM ai_trace_runs r LEFT JOIN photos p ON p.id=r.photo_id
                WHERE r.trace_id=?
                """,
                (str(trace_id)[:160],),
            ).fetchone()
            if run is None:
                return None
            attempts = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT a.*,u.input_tokens,u.output_tokens,u.cached_tokens,u.reasoning_tokens,
                           u.cache_write_tokens,u.estimated_cost,u.actual_cost,u.cost_source,
                           u.request_id usage_request_id,u.latency_ms usage_latency_ms
                    FROM ai_trace_attempts a LEFT JOIN api_usage u ON u.id=a.api_usage_id
                    WHERE a.trace_id=? ORDER BY a.attempt_number,a.id
                    """,
                    (trace_id,),
                ).fetchall()
            ]
            activity = [
                dict(row)
                for row in connection.execute(
                    "SELECT event,message,severity,created_at FROM activity_events "
                    "WHERE trace_id=? ORDER BY created_at,id LIMIT 200",
                    (trace_id,),
                ).fetchall()
            ]
        result = dict(run)
        for name in ("final_result_json", "exif_json"):
            if result.get(name):
                try:
                    result[name] = json.loads(result[name])
                except (TypeError, ValueError, json.JSONDecodeError):
                    result[name] = None
        for attempt in attempts:
            for name in ("request_json_sanitized", "response_parsed_json"):
                if attempt.get(name):
                    try:
                        attempt[name] = json.loads(attempt[name])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        attempt[name] = None
            if not include_sensitive:
                attempt.pop("request_json_sanitized", None)
                attempt.pop("response_raw_sanitized", None)
        result["attempts"] = attempts
        result["activity_events"] = activity
        return sanitize_trace_value(result)
