"""Bounded, read-only cost scenarios from retained photo Token measurements."""
from __future__ import annotations

from inktime.app.providers.base import Usage
from inktime.app.providers.openai_compatible import calculate_usage_cost
from inktime.app.providers.openai_models import DEFAULT_OPENAI_MODEL, official_openai_pricing


def openai_cost_projection(database, *, model: str = DEFAULT_OPENAI_MODEL, remaining: int | None = None) -> dict:
    prices = official_openai_pricing().get(model)
    result = {"model": model, "currency": "USD", "sample_photos": 0, "basis": "no_measurements",
              "average_input_tokens": None, "average_output_tokens": None, "average_reasoning_tokens": None,
              "standard_per_photo": None, "batch_per_photo": None, "scenarios": [],
              "remaining_photos": remaining, "remaining_standard": None, "remaining_batch": None,
              "billing_balance": None, "billing_message": "需至 OpenAI Billing 查詢"}
    if prices is None:
        return result
    with database.session() as connection:
        matched = bool(connection.execute(
            "SELECT 1 FROM api_usage u JOIN providers p ON p.id=u.provider_id "
            "WHERE p.kind='openai' AND u.model=? AND u.photo_id IS NOT NULL "
            "AND u.status='completed' AND u.usage_complete=1 LIMIT 1", (model,),
        ).fetchone())
        # At most 500 recent photos, including their repairs; no photo bytes,
        # filesystem paths or private response content are loaded by this query.
        rows = connection.execute(
            "WITH samples AS (SELECT photo_id,MAX(started_at) latest FROM api_usage "
            "WHERE photo_id IS NOT NULL AND status='completed' AND usage_complete=1 "
            "AND NOT EXISTS (SELECT 1 FROM api_usage bad WHERE bad.photo_id=api_usage.photo_id AND bad.usage_complete=0) "
            "AND (?=0 OR (model=? AND provider_id IN (SELECT id FROM providers WHERE kind='openai'))) "
            "GROUP BY photo_id ORDER BY latest DESC LIMIT 500) "
            "SELECT u.photo_id,u.input_tokens,u.output_tokens,u.cached_tokens,u.reasoning_tokens,u.cache_write_tokens "
            "FROM api_usage u JOIN samples s ON s.photo_id=u.photo_id "
            "WHERE u.status='completed' AND u.usage_complete=1 "
            "AND (?=0 OR (u.model=? AND u.provider_id IN (SELECT id FROM providers WHERE kind='openai')))",
            (int(matched), model, int(matched), model),
        ).fetchall()
    photo_ids = {row["photo_id"] for row in rows}
    if not photo_ids:
        return result
    total_standard, total_batch = 0.0, 0.0
    for row in rows:
        usage = Usage(input_tokens=int(row["input_tokens"]), output_tokens=int(row["output_tokens"]),
                      cached_tokens=int(row["cached_tokens"]) if matched else 0,
                      reasoning_tokens=int(row["reasoning_tokens"]),
                      cache_write_tokens=int(row["cache_write_tokens"]) if matched else 0)
        standard = calculate_usage_cost(prices, usage)
        batch = calculate_usage_cost(prices, usage, batch=True)
        if standard is None or batch is None:
            return result
        total_standard += standard
        total_batch += batch
    count = len(photo_ids)
    standard_each, batch_each = total_standard / count, total_batch / count
    result.update(sample_photos=count, basis="same_openai_model" if matched else "other_models_scenario",
                  average_input_tokens=sum(int(r["input_tokens"]) for r in rows) / count,
                  average_output_tokens=sum(int(r["output_tokens"]) for r in rows) / count,
                  average_reasoning_tokens=sum(int(r["reasoning_tokens"]) for r in rows) / count,
                  standard_per_photo=standard_each, batch_per_photo=batch_each,
                  scenarios=[{"photos": n, "standard": standard_each * n, "batch": batch_each * n}
                             for n in (10_000, 100_000)],
                  remaining_standard=standard_each * remaining if remaining is not None else None,
                  remaining_batch=batch_each * remaining if remaining is not None else None)
    return result
