from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone

from inktime.app.db import Database


class UsageRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _registered_provider_id(connection, provider_id: str | None) -> str | None:
        """Keep the optional identity link valid for external/test providers."""

        if not provider_id:
            return None
        row = connection.execute("SELECT id FROM providers WHERE id=?", (str(provider_id),)).fetchone()
        return str(row[0]) if row is not None else None

    def record(
        self,
        *,
        provider: str,
        provider_id: str | None = None,
        model: str,
        job_id: str | None,
        photo_id: str | None,
        request_type: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        estimated_cost: float | None,
        actual_cost: float | None,
        started_at: str,
        latency_ms: int,
        status: str,
        retry_count: int = 0,
        error_code: str | None = None,
        batch_id: str | None = None,
        batch_item_id: str | None = None,
        processing_mode: str = "sync",
        request_id: str | None = None,
        reasoning_tokens: int = 0,
        cache_write_tokens: int = 0,
        cost_source: str = "unknown",
        prompt_chars: int = 0,
        schema_chars: int = 0,
        request_body_bytes: int = 0,
        image_bytes: int = 0,
    ) -> None:
        completed_at = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            registered_provider_id = self._registered_provider_id(connection, provider_id)
            connection.execute(
                """
                INSERT INTO api_usage(provider,provider_id,model,job_id,photo_id,request_type,input_tokens,output_tokens,
                    cached_tokens,estimated_cost,actual_cost,started_at,completed_at,latency_ms,status,retry_count,error_code,
                    batch_id,batch_item_id,processing_mode,request_id,reasoning_tokens,cache_write_tokens,cost_source,
                    prompt_chars,schema_chars,request_body_bytes,image_bytes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    provider,
                    registered_provider_id,
                    model,
                    job_id,
                    photo_id,
                    request_type,
                    input_tokens,
                    output_tokens,
                    cached_tokens,
                    max(0.0, float(estimated_cost)) if estimated_cost is not None else None,
                    actual_cost,
                    started_at,
                    completed_at,
                    latency_ms,
                    status,
                    retry_count,
                    error_code,
                    batch_id,
                    batch_item_id,
                    processing_mode,
                    request_id,
                    reasoning_tokens,
                    max(0, int(cache_write_tokens)),
                    cost_source if cost_source in {"provider_reported", "estimated", "unknown"} else "unknown",
                    max(0, int(prompt_chars)),
                    max(0, int(schema_chars)),
                    max(0, int(request_body_bytes)),
                    max(0, int(image_bytes)),
                ),
            )

    def record_batch_once(
        self,
        *,
        provider: str,
        provider_id: str | None = None,
        model: str,
        job_id: str | None,
        photo_id: str | None,
        batch_id: str,
        batch_item_id: str,
        request_type: str,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
        estimated_cost: float | None,
        actual_cost: float | None,
        request_id: str | None,
        started_at: str,
        status: str = "completed",
        connection=None,
        cache_write_tokens: int = 0,
        cost_source: str = "unknown",
    ) -> bool:
        """Record one Batch item exactly once; the migration enforces the same invariant."""

        completed_at = datetime.now(timezone.utc).isoformat()
        context = self.database.session() if connection is None else nullcontext(connection)
        with context as active_connection:
            connection = active_connection
            registered_provider_id = self._registered_provider_id(connection, provider_id)
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO api_usage(
                    provider,provider_id,model,job_id,photo_id,request_type,input_tokens,output_tokens,cached_tokens,
                    estimated_cost,actual_cost,started_at,completed_at,latency_ms,status,retry_count,error_code,
                    batch_id,batch_item_id,processing_mode,request_id,reasoning_tokens,cache_write_tokens,cost_source
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    provider,
                    registered_provider_id,
                    model,
                    job_id,
                    photo_id,
                    request_type,
                    max(0, int(input_tokens)),
                    max(0, int(output_tokens)),
                    max(0, int(cached_tokens)),
                    max(0.0, float(estimated_cost)) if estimated_cost is not None else 0.0,
                    max(0.0, float(actual_cost)) if actual_cost is not None else None,
                    started_at,
                    completed_at,
                    0,
                    status,
                    0,
                    None,
                    batch_id,
                    batch_item_id,
                    "batch",
                    request_id,
                    max(0, int(reasoning_tokens)),
                    max(0, int(cache_write_tokens)),
                    cost_source if cost_source in {"provider_reported", "estimated", "unknown"} else "unknown",
                ),
            )
        return bool(cursor.rowcount)
