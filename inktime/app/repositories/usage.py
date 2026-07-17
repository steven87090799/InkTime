from __future__ import annotations

from datetime import datetime, timezone

from inktime.app.db import Database


class UsageRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(
        self,
        *,
        provider: str,
        model: str,
        job_id: str | None,
        photo_id: str | None,
        request_type: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        estimated_cost: float,
        actual_cost: float | None,
        started_at: str,
        latency_ms: int,
        status: str,
        retry_count: int = 0,
        error_code: str | None = None,
    ) -> None:
        completed_at = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute(
                """
                INSERT INTO api_usage(provider,model,job_id,photo_id,request_type,input_tokens,output_tokens,
                    cached_tokens,estimated_cost,actual_cost,started_at,completed_at,latency_ms,status,retry_count,error_code)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    provider,
                    model,
                    job_id,
                    photo_id,
                    request_type,
                    input_tokens,
                    output_tokens,
                    cached_tokens,
                    estimated_cost,
                    actual_cost,
                    started_at,
                    completed_at,
                    latency_ms,
                    status,
                    retry_count,
                    error_code,
                ),
            )
