"""Persistent, restart-safe OpenAI Batch analysis lifecycle.

The service deliberately keeps remote state in SQLite and treats every output
line as an independently auditable item.  It never waits on a remote Batch in
the worker that submitted it.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from datetime import datetime
from pathlib import Path
import re
import resource
import sqlite3
import time
from typing import Any, Callable, Iterable
from uuid import uuid4

from inktime.app.core.paths import safe_join
from inktime.app.domain.analysis import (
    AnalysisValidationError,
    canonical_json,
    fingerprint,
    normalize_reasoning_effort,
    validate_analysis_result,
)
from inktime.app.domain.analysis.plan import SCHEMA_VERSION
from inktime.app.providers.base import Usage
from inktime.app.repositories.analysis_batches import (
    ACTIVE_BATCH_STATUSES,
    TERMINAL_BATCH_STATUSES,
    AnalysisBatchRepository,
    utc_now,
)


MAX_OPENAI_REQUESTS = 50_000
MAX_OPENAI_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_ITEMS = 500
DEFAULT_MAX_BYTES = 150 * 1024 * 1024
CUSTOM_ID_RE = re.compile(r"^ibt:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REMOTE_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")


class BatchLifecycleError(RuntimeError):
    def __init__(self, message: str, code: str = "BATCH-001") -> None:
        super().__init__(message)
        self.code = code


def _peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss or 0)
    # Linux reports KiB; macOS reports bytes.
    return value * 1024 if value < 10_000_000 else value


def stream_jsonl_shards(
    root: Path,
    items: Iterable[dict[str, Any]],
    line_factory: Callable[[dict[str, Any]], bytes],
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    path_prefix: str = "input",
) -> list[dict[str, Any]]:
    """Write JSONL one item at a time and split by actual bytes.

    Only shard metadata is retained in memory.  The request body, encoded
    image, JSON line, and source thumbnail can therefore be released after one
    item is written.
    """

    max_items = max(1, min(int(max_items), MAX_OPENAI_REQUESTS))
    max_bytes = max(1, min(int(max_bytes), MAX_OPENAI_BYTES))
    root = root.resolve()
    shards: list[dict[str, Any]] = []
    handle = None
    current: dict[str, Any] | None = None

    def close_current() -> None:
        nonlocal handle, current
        if handle is None or current is None:
            return
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.chmod(Path(current["path"]), 0o600)
        current["peak_rss_bytes"] = _peak_rss_bytes()
        shards.append(current)
        handle = None
        current = None

    try:
        for item in items:
            line = line_factory(item)
            if not isinstance(line, bytes):
                raise TypeError("Batch JSONL line_factory 必須回傳 bytes")
            if not line.endswith(b"\n"):
                line += b"\n"
            line_bytes = len(line)
            if line_bytes > max_bytes:
                raise BatchLifecycleError(
                    f"單一 Batch Request 超過安全 JSONL 分片上限：{line_bytes} bytes",
                    "BATCH-INPUT-TOO-LARGE",
                )
            if (
                current is None
                or int(current["items_count"]) >= max_items
                or int(current["bytes"]) + line_bytes > max_bytes
            ):
                close_current()
                shard_number = len(shards) + 1
                directory = root / f"{shard_number:04d}"
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f"{path_prefix}.jsonl"
                handle = path.open("wb")
                os.chmod(path, 0o600)
                current = {
                    "path": str(path),
                    "items": [],
                    "items_count": 0,
                    "bytes": 0,
                    "peak_rss_bytes": 0,
                }
            assert handle is not None and current is not None
            handle.write(line)
            current["items"].append(str(item["id"]))
            current["items_count"] = int(current["items_count"]) + 1
            current["bytes"] = int(current["bytes"]) + line_bytes
            del line
        close_current()
    except Exception:
        if handle is not None:
            handle.close()
        raise
    return shards


class BatchAnalysisService:
    ENDPOINT = "/v1/chat/completions"

    def __init__(
        self,
        database,
        batches: AnalysisBatchRepository,
        jobs,
        job_service,
        photos,
        providers,
        provider_service,
        usage,
        thumbnails,
        analysis,
        settings,
        data_dir: Path,
        observability=None,
        scoring_repository=None,
    ) -> None:
        self.database = database
        self.batches = batches
        self.jobs = jobs
        self.job_service = job_service
        self.photos = photos
        self.providers = providers
        self.provider_service = provider_service
        self.usage = usage
        self.thumbnails = thumbnails
        self.analysis = analysis
        self.settings = settings
        self.data_dir = data_dir.resolve()
        self.batch_root = self.data_dir / "batches"
        self.batch_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.batch_root, 0o700)
        self.observability = observability
        self.scoring_repository = scoring_repository

    def _activity(self, severity: str, event: str, message: str, **fields: Any) -> None:
        if self.observability is not None:
            safe_fields = {
                key: fields[key]
                for key in ("batch_id", "batch_item_id", "job_id", "count", "status", "error_code")
                if key in fields
            }
            self.observability.record(severity, "analysis_batch", event, message, **safe_fields)

    def _batch_limits(self) -> tuple[int, int]:
        return (
            int(self.settings.get("batch.max_items_per_shard", DEFAULT_MAX_ITEMS)),
            int(self.settings.get("batch.max_jsonl_bytes", DEFAULT_MAX_BYTES)),
        )

    def _provider_route(self) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.providers.list()
            if bool(row.get("enabled")) and bool(row.get("supports_batch"))
        ]
        rows.sort(key=lambda row: (int(row.get("priority") or 100), str(row.get("name") or row["id"])))
        if not rows:
            raise BatchLifecycleError("沒有已啟用且支援 Batch 的 Provider", "BATCH-PROVIDER-001")
        row = rows[0]
        snapshot = self.provider_service.route_snapshot()
        selected = next((item for item in snapshot if str(item["provider_id"]) == str(row["id"])), None)
        if selected is None:
            raise BatchLifecycleError("Batch Provider 不在目前的 Frozen Route", "BATCH-PROVIDER-002")
        return [selected]

    def _plan(self, route: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], str, str]:
        route = route or self._provider_route()
        # The scoring repository is intentionally read through the app graph by
        # bootstrap; this fallback keeps direct service tests dependency-light.
        scoring = getattr(self, "scoring", None) or {
            "id": "",
            "memory_weight": 25,
            "beauty_weight": 25,
            "technical_weight": 25,
            "emotion_weight": 25,
            "favorite_bonus": 0,
        }
        scoring_repository = getattr(self, "scoring_repository", None)
        if scoring_repository is not None:
            scoring = dict(scoring_repository.current())
        plan = self.analysis.build_plan(
            strategy="high_quality",
            provider_route=route,
            scoring_profile=scoring,
        )
        model = str(self.settings.get("batch.model", "gpt-5.6-luna")).strip()
        if not model:
            raise BatchLifecycleError("Batch 模型不可空白", "BATCH-MODEL-001")
        # Batch is a single full analysis.  The legacy smart_two_stage path is
        # not used and no second model call is made during import.
        plan["strategy"] = "high_quality"
        plan["low_model"] = model
        plan["high_model"] = model
        plan["processing_mode"] = "batch"
        plan["batch_endpoint"] = self.ENDPOINT
        plan["batch_schema_kind"] = "full"
        plan["batch_completion_window"] = "24h"
        plan["reasoning_effort"] = normalize_reasoning_effort(
            self.settings.get("batch.reasoning_effort", "none")
        )
        return plan, fingerprint(plan), model

    def _base_candidate_sql(self) -> tuple[str, list[Any]]:
        active = sorted(ACTIVE_BATCH_STATUSES)
        marks = ",".join("?" for _ in active)
        return (
            f"""
            SELECT p.*,l.root_path
            FROM photos p JOIN libraries l ON l.id=p.library_id
            WHERE p.lifecycle_status='active' AND p.eligible=1
              AND COALESCE(p.never_upload,0)=0
              AND COALESCE(p.exclusion_status,'')!='manually_excluded'
              AND COALESCE(p.sha256,'')!=''
              AND NOT EXISTS (
                  SELECT 1 FROM photo_analysis a
                  WHERE a.photo_id=p.id AND a.analysis_fingerprint=? AND COALESCE(a.schema_kind,'basic')='full'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM analysis_batch_items i
                  JOIN analysis_batches b ON b.id=i.batch_id
                  WHERE i.photo_id=p.id AND b.status IN ({marks})
              )
            ORDER BY COALESCE(p.local_candidate_score,-1) DESC,
                     COALESCE(p.captured_at,p.created_at),p.id
            """,  # noqa: S608 -- only the placeholder count is dynamic.
            active,
        )

    def _candidate_rows(
        self,
        *,
        scope: str,
        analysis_fingerprint: str,
        provider_id: str,
        model: str,
        plan: dict[str, Any],
        photo_ids: Iterable[str] | None,
        sample_count: int,
    ) -> tuple[list[dict[str, Any]], dict[str, int], str | None]:
        query, active_parameters = self._base_candidate_sql()
        parameters: list[Any] = [analysis_fingerprint, *active_parameters]
        if scope == "manual_selection":
            requested = list(dict.fromkeys(str(value) for value in (photo_ids or [])))
            if not requested:
                return [], {"never_upload_excluded": 0, "cache_hits": 0, "sha_duplicates": 0}, None
            marks = ",".join("?" for _ in requested)
            query = query.replace(
                "ORDER BY COALESCE(p.local_candidate_score,-1) DESC,\n                     COALESCE(p.captured_at,p.created_at),p.id",
                f"AND p.id IN ({marks})\n            ORDER BY COALESCE(p.local_candidate_score,-1) DESC,\n                     COALESCE(p.captured_at,p.created_at),p.id",
            )
            parameters.extend(requested)
        with self.database.session() as connection:
            rows = [dict(row) for row in connection.execute(query, parameters).fetchall()]
            never_upload = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM photos p
                    WHERE p.lifecycle_status='active' AND p.eligible=1 AND COALESCE(p.never_upload,0)=1
                    """
                ).fetchone()[0]
            )
        safe_rows: list[dict[str, Any]] = []
        for row in rows:
            try:
                source = safe_join(Path(str(row["root_path"])), str(row["relative_path"]))
            except (OSError, ValueError):
                continue
            if not source.is_file():
                continue
            safe_rows.append({"photo": row, "source": source})
        seed: str | None = None
        if scope == "sample":
            seed = hashlib.sha256(f"{scope}:{analysis_fingerprint}".encode("utf-8")).hexdigest()
            safe_rows.sort(
                key=lambda item: hashlib.sha256(f"{seed}:{item['photo']['id']}".encode("utf-8")).hexdigest()
            )
            safe_rows = safe_rows[: max(1, min(int(sample_count), 100_000))]
        candidates: list[dict[str, Any]] = []
        seen_sha: set[str] = set()
        cache_hits = 0
        sha_duplicates = 0
        vision_input = dict(plan["high_vision_input"])
        vision_input["schema_kind"] = "full"
        vision_input["reasoning_effort"] = str(plan["reasoning_effort"])
        for item in safe_rows:
            photo = item["photo"]
            content_sha = str(photo["sha256"] or "").casefold()
            if content_sha in seen_sha:
                sha_duplicates += 1
                continue
            seen_sha.add(content_sha)
            vision_fp = fingerprint(
                {
                    "content_sha256": content_sha,
                    "actual_provider": provider_id,
                    "model": model,
                    "prompt_version": str(plan["prompt_version"]),
                    "schema_version": SCHEMA_VERSION,
                    "schema_kind": "full",
                    "reasoning_effort": str(plan["reasoning_effort"]),
                    **vision_input,
                }
            )
            cached = self.photos.get_ai_cache(
                content_sha256=content_sha,
                provider=provider_id,
                model_name=model,
                prompt_version=str(plan["prompt_version"]),
                schema_version=SCHEMA_VERSION,
                schema_kind="full",
                vision_request_fingerprint=vision_fp,
            )
            if cached is not None:
                try:
                    validate_analysis_result(cached["result"])
                except (AnalysisValidationError, TypeError, ValueError):
                    pass
                else:
                    cache_hits += 1
                    continue
            candidates.append(
                {
                    "photo": photo,
                    "source": item["source"],
                    "content_sha256": content_sha,
                    "vision_request_fingerprint": vision_fp,
                    "vision_input_spec_json": canonical_json(vision_input),
                }
            )
        return (
            candidates,
            {
                "never_upload_excluded": never_upload,
                "cache_hits": cache_hits,
                "sha_duplicates": sha_duplicates,
            },
            seed,
        )

    def estimate(
        self,
        *,
        scope: str = "sample",
        sample_count: int = 100,
        photo_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        if scope not in {"sample", "all_eligible_missing_analysis", "new_or_changed", "manual_selection"}:
            raise BatchLifecycleError("不支援的 Batch scope", "BATCH-SCOPE-001")
        plan, analysis_fp, model = self._plan()
        route = plan["provider_route"]
        provider_id = str(route[0]["provider_id"])
        candidates, skipped, seed = self._candidate_rows(
            scope=scope,
            analysis_fingerprint=analysis_fp,
            provider_id=provider_id,
            model=model,
            plan=plan,
            photo_ids=photo_ids,
            sample_count=sample_count,
        )
        max_items, max_bytes = self._batch_limits()
        # This is deliberately an estimate; the submit path records actual
        # shard bytes after ThumbnailCache and JSON serialization.
        estimated_input_tokens = len(candidates) * int(
            self.settings.get("batch.estimated_input_tokens", 2500)
        )
        estimated_output_tokens = len(candidates) * int(
            self.settings.get("batch.estimated_output_tokens", 500)
        )
        pricing = self.providers.pricing(provider_id).get(model, {})
        input_price = float(
            pricing.get("batch_input_per_million")
            or pricing.get("input_per_million", 0) * float(pricing.get("batch_multiplier", 0.5) or 0.5)
        )
        output_price = float(
            pricing.get("batch_output_per_million")
            or pricing.get("output_per_million", 0) * float(pricing.get("batch_multiplier", 0.5) or 0.5)
        )
        estimated_cost = (
            estimated_input_tokens * input_price + estimated_output_tokens * output_price
        ) / 1_000_000
        return {
            "scope": scope,
            "sample_seed": seed,
            "candidate_count": len(candidates),
            "cache_hits": skipped["cache_hits"],
            "sha_duplicates": skipped["sha_duplicates"],
            "never_upload_excluded": skipped["never_upload_excluded"],
            "submitted_count": len(candidates),
            "estimated_jsonl_bytes": min(MAX_OPENAI_BYTES, len(candidates) * 350_000),
            "estimated_shard_count": max(0, (len(candidates) + max_items - 1) // max_items),
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_output_tokens": estimated_output_tokens,
            "estimated_cost": round(estimated_cost, 6),
            "model": model,
            "analysis_fingerprint": analysis_fp,
            "max_items_per_shard": max_items,
            "max_jsonl_bytes": max_bytes,
        }

    def _provider(self, provider_id: str, plan: dict[str, Any]):
        route = [item for item in plan["provider_route"] if str(item["provider_id"]) == provider_id]
        if not route:
            raise BatchLifecycleError("Frozen Batch Provider 不存在", "BATCH-PROVIDER-003")
        provider = self.provider_service.build_router(
            route,
            scoring_rules=str(plan.get("scoring_rules") or ""),
            caption_controls=plan.get("caption_controls") or None,
        )
        if provider is None:
            raise BatchLifecycleError("Frozen Batch Provider 無法建立", "BATCH-PROVIDER-004")
        return provider

    def _cleanup_plan(self, batch: dict[str, Any]) -> dict[str, Any]:
        """Build a non-analysis fallback route solely to clean known files."""

        route = [
            item
            for item in self.provider_service.route_snapshot()
            if str(item.get("provider_id")) == str(batch["provider_id"])
        ]
        if not route:
            raise BatchLifecycleError(
                "原 Provider route 已不存在，無法安全清理遠端檔案", "BATCH-CLEANUP-PROVIDER-001"
            )
        return {"provider_route": route, "scoring_rules": ""}

    def _line_factory(self, provider, plan: dict[str, Any]) -> Callable[[dict[str, Any]], bytes]:
        max_tokens = int(self.settings.get("budget.max_tokens", 8000))

        def make_line(item: dict[str, Any]) -> bytes:
            body = provider.build_analysis_request_body(
                image_path=item["thumbnail"],
                model=str(plan["high_model"]),
                detail=str(plan["high_vision_input"]["detail"]),
                stage="single_high",
                max_tokens=max_tokens,
                caption_controls=plan.get("caption_controls") or None,
                reasoning_effort=str(plan["reasoning_effort"]),
            )
            payload = {
                "custom_id": str(item["custom_id"]),
                "method": "POST",
                "url": self.ENDPOINT,
                "body": body,
            }
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"

        return make_line

    def _prepare_shards(
        self, batch_id: str, candidates: list[dict[str, Any]], plan: dict[str, Any], provider
    ) -> list[str]:
        items = self.batches.items(batch_id)
        by_photo = {str(item["photo_id"]): item for item in items}
        stream_items: list[dict[str, Any]] = []
        for candidate in candidates:
            db_item = by_photo[str(candidate["photo"]["id"])]
            stream_items.append({**candidate, **db_item})

        def factory(item: dict[str, Any]) -> bytes:
            source = Path(str(item["source"]))
            with self.thumbnails.acquire_for_use(
                source,
                str(item["content_sha256"]),
                int(plan["high_vision_input"]["max_side"]),
            ) as thumbnail:
                item["thumbnail"] = thumbnail
                try:
                    return self._line_factory(provider, plan)(item)
                finally:
                    item.pop("thumbnail", None)

        max_items, max_bytes = self._batch_limits()
        shards = stream_jsonl_shards(
            self.batch_root / batch_id,
            stream_items,
            factory,
            max_items=max_items,
            max_bytes=max_bytes,
        )
        batch_ids: list[str] = []
        for index, shard in enumerate(shards):
            current_id = batch_id if index == 0 else str(uuid4())
            path = Path(str(shard["path"]))
            if index > 0:
                child_directory = self.batch_root / current_id / path.parent.name
                child_directory.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path.parent, child_directory)
                path = child_directory / path.name
                self.batches.create_child_batch(
                    batch_id,
                    current_id,
                    shard["items"],
                    local_input_path=str(path),
                    total_items=int(shard["items_count"]),
                    peak_rss_bytes=int(shard["peak_rss_bytes"]),
                )
            else:
                self.batches.update_batch(
                    batch_id,
                    local_input_path=str(path),
                    total_items=int(shard["items_count"]),
                    peak_rss_bytes=int(shard["peak_rss_bytes"]),
                )
            batch_ids.append(current_id)
        return batch_ids

    def _submit_one(self, batch_id: str, plan: dict[str, Any]) -> None:
        batch = self.batches.get(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        if str(batch["status"]) in {"upload_unknown", "submission_unknown"}:
            raise BatchLifecycleError(
                "Batch 外部 side effect 結果未知，必須先由管理員 Recovery 或 Abandon",
                "BATCH-UNKNOWN-HOLD",
            )
        provider = self._provider(str(batch["provider_id"]), plan)
        try:
            current = self.batches.get(batch_id)
            if current is None:
                raise KeyError(batch_id)
            if not current["input_file_id"]:
                attempt_id = str(uuid4())
                self.batches.update_batch(
                    batch_id,
                    status="uploading",
                    upload_attempt_id=attempt_id,
                    phase_started_at=utc_now(),
                    last_error_code=None,
                    last_error_message=None,
                )
                input_path = Path(str(current["local_input_path"] or ""))
                if not input_path.is_file():
                    raise BatchLifecycleError("找不到待上傳的 JSONL 分片", "BATCH-INPUT-002")
                upload_method = provider.upload_batch_file
                parameters = inspect.signature(upload_method).parameters
                if "remote_filename" in parameters or any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
                ):
                    input_file_id = upload_method(
                        input_path, remote_filename=f"inktime-batch-{batch_id}.jsonl"
                    )
                else:  # compatibility with older test and third-party providers
                    input_file_id = upload_method(input_path)
                if not isinstance(input_file_id, str) or not input_file_id:
                    raise BatchLifecycleError("上傳回應缺少 file id", "BATCH-UPLOAD-UNKNOWN")
                self.batches.update_batch(
                    batch_id,
                    input_file_id=input_file_id,
                    status="uploaded",
                    phase_started_at=utc_now(),
                )
            current = self.batches.get(batch_id)
            if current is None or not current["input_file_id"]:
                raise BatchLifecycleError("Batch 缺少已保存的 input_file_id", "BATCH-UPLOAD-UNKNOWN")
            attempt_id = str(uuid4())
            self.batches.update_batch(
                batch_id,
                status="submitting",
                submission_attempt_id=attempt_id,
                phase_started_at=utc_now(),
                submitted_at=utc_now(),
            )
            remote = provider.create_batch(
                str(current["input_file_id"]),
                completion_window="24h",
                metadata={"inktime_batch_id": batch_id, "inktime_version": "batch-lifecycle-v1"},
                output_expires_after_seconds=int(
                    self.settings.get("batch.output_expires_after_seconds", 86400)
                ),
            )
            remote_id = remote.get("id")
            if not isinstance(remote_id, str) or not remote_id:
                raise BatchLifecycleError("遠端 Batch 建立回應缺少 id", "BATCH-SUBMISSION-UNKNOWN")
            self.batches.bind_recovered_remote(batch_id, remote)
            self._activity("INFO", "batch_submitted", "Batch 已提交", batch_id=batch_id, status="validating")
        except Exception as exc:
            current = self.batches.get(batch_id)
            has_uploaded_file = bool(current is not None and current["input_file_id"])
            code = str(getattr(exc, "code", "BATCH-SUBMIT-001"))
            ambiguous = bool(getattr(exc, "ambiguous", False)) or code in {
                "BATCH-SUBMISSION-UNKNOWN",
                "BATCH-UPLOAD-UNKNOWN",
            }
            upload_unknown = code == "BATCH-UPLOAD-UNKNOWN" or (
                ambiguous and current is not None and str(current["status"]) == "uploading"
            )
            if ambiguous:
                next_status = "upload_unknown" if upload_unknown else "submission_unknown"
                self.batches.update_batch(
                    batch_id,
                    status=next_status,
                    remote_status=next_status,
                    cleanup_status="pending" if has_uploaded_file else "not_required",
                    last_error_code=next_status,
                    last_error_message=str(exc)[:1000],
                    phase_started_at=utc_now(),
                )
            elif has_uploaded_file:
                # A definite HTTP rejection leaves the uploaded input file to
                # the cleanup worker; it is never silently retried as a new Batch.
                self.batches.update_batch(
                    batch_id,
                    status="cleanup_pending",
                    remote_status="failed",
                    cleanup_status="pending",
                    last_error_code=code,
                    last_error_message=str(exc)[:1000],
                    phase_started_at=utc_now(),
                )
            else:
                self.batches.update_batch(
                    batch_id,
                    status="failed",
                    remote_status="failed",
                    cleanup_status="not_required",
                    last_error_code=code,
                    last_error_message=str(exc)[:1000],
                    phase_started_at=utc_now(),
                )
            for item in self.batches.items(batch_id):
                if str(item["status"]) == "pending":
                    self.batches.update_item(
                        str(item["id"]),
                        status=("upload_unknown" if upload_unknown else "submission_unknown")
                        if ambiguous
                        else ("retry_pending" if has_uploaded_file else "failed"),
                        error_code=("upload_unknown" if upload_unknown else "submission_unknown")
                        if ambiguous
                        else "submit_failed",
                    )
            if not ambiguous and not has_uploaded_file:
                self._finish(batch_id)
            raise
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    def submit(
        self,
        *,
        scope: str = "sample",
        sample_count: int = 100,
        photo_ids: Iterable[str] | None = None,
        created_by: str | None = None,
        budget_limit: float | None = None,
    ) -> dict[str, Any]:
        plan, analysis_fp, model = self._plan()
        provider_id = str(plan["provider_route"][0]["provider_id"])
        candidates, skipped, seed = self._candidate_rows(
            scope=scope,
            analysis_fingerprint=analysis_fp,
            provider_id=provider_id,
            model=model,
            plan=plan,
            photo_ids=photo_ids,
            sample_count=sample_count,
        )
        if not candidates:
            raise BatchLifecycleError("目前沒有符合條件的 Batch 候選", "BATCH-CANDIDATE-001")
        estimate = self.estimate(scope=scope, sample_count=sample_count, photo_ids=photo_ids)
        if budget_limit is not None and estimate["estimated_cost"] > float(budget_limit):
            raise BatchLifecycleError("整批估算成本超過 Job Budget，未提交任何分片", "BATCH-BUDGET-001")
        job_id = self.jobs.create(
            kind="analysis_batch",
            name=f"OpenAI Batch：{scope}",
            strategy="high_quality",
            settings={"processing_mode": "batch", "scope": scope, "sample_seed": seed},
            photo_ids=[str(item["photo"]["id"]) for item in candidates],
            created_by=created_by,
            budget_limit=budget_limit,
            selection_mode=scope,
            analysis_fingerprint=analysis_fp,
            analysis_spec=plan,
        )
        job_items = self.jobs.list_items(job_id, limit=max(100, len(candidates)))
        job_items_by_photo = {str(item["photo_id"]): item for item in job_items}
        estimated_each = float(estimate["estimated_cost"] or 0) / max(1, len(candidates))
        batch_id = str(uuid4())
        item_rows = []
        for candidate in candidates:
            photo_id = str(candidate["photo"]["id"])
            item_rows.append(
                {
                    "id": str(uuid4()),
                    "job_item_id": str(job_items_by_photo[photo_id]["id"]),
                    "photo_id": photo_id,
                    "custom_id": f"ibt:{uuid4()}",
                    "content_sha256": candidate["content_sha256"],
                    "analysis_fingerprint": analysis_fp,
                    "vision_request_fingerprint": candidate["vision_request_fingerprint"],
                    "vision_input_spec_json": candidate["vision_input_spec_json"],
                    "estimated_cost": estimated_each,
                }
            )
        try:
            self.batches.create_with_items(
                {
                    "id": batch_id,
                    "job_id": job_id,
                    "provider_id": provider_id,
                    "model": model,
                    "endpoint": self.ENDPOINT,
                    "analysis_fingerprint": analysis_fp,
                    "estimated_cost": estimate["estimated_cost"],
                    "sample_seed": seed,
                    "candidate_snapshot_json": self.batches.snapshot_json(
                        [str(item["photo"]["id"]) for item in candidates]
                    ),
                    "scope": scope,
                },
                item_rows,
            )
        except sqlite3.IntegrityError as exc:
            # The partial unique reservation indexes serialize concurrent
            # submits.  The losing Job was never started and is explicitly
            # closed so it cannot become an orphan pending/running Job.
            self.jobs.abandon_unstarted(job_id)
            raise BatchLifecycleError(
                "候選照片已被另一個 Batch reservation 取得，未建立第二個遠端 Batch",
                "BATCH-RESERVATION-CONFLICT",
            ) from exc
        try:
            self.job_service.start(job_id)
        except Exception as exc:
            self.jobs.abandon_unstarted(job_id, "BATCH-JOB-START-001")
            self.batches.update_batch(
                batch_id,
                status="failed",
                remote_status="failed",
                cleanup_status="not_required",
                last_error_code="BATCH-JOB-START-001",
                last_error_message=str(exc)[:1000],
            )
            self._finish(batch_id)
            raise
        provider = self._provider(provider_id, plan)
        try:
            batch_ids = self._prepare_shards(batch_id, candidates, plan, provider)
        except Exception as exc:
            self.batches.update_batch(
                batch_id,
                status="failed",
                remote_status="failed",
                cleanup_status="not_required",
                last_error_code=str(getattr(exc, "code", "BATCH-INPUT-001")),
                last_error_message=str(exc)[:1000],
            )
            self._finish(batch_id)
            raise
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        submitted: list[str] = []
        for current_id in batch_ids:
            current = self.batches.get(current_id)
            if current is None:
                continue
            try:
                self._submit_one(current_id, plan)
                submitted.append(current_id)
            except Exception as exc:
                self._activity(
                    "WARNING",
                    "batch_submit_failed",
                    "Batch 分片提交失敗；保留待重試狀態",
                    batch_id=current_id,
                    error_code=str(getattr(exc, "code", "BATCH-SUBMIT-001")),
                )
                continue
        return {
            "job_id": job_id,
            "batch_ids": submitted,
            "prepared_batch_ids": batch_ids,
            "candidate_count": len(candidates),
            "cache_hits": skipped["cache_hits"],
            "sha_duplicates": skipped["sha_duplicates"],
            "never_upload_excluded": skipped["never_upload_excluded"],
            "analysis_fingerprint": analysis_fp,
            "model": model,
        }

    def _enqueue_import(self, batch_id: str, *, cleanup_only: bool = False) -> str:
        job_id = self.jobs.create_maintenance(
            kind="analysis_batch_import",
            name=f"匯入 OpenAI Batch {batch_id}",
            settings={"batch_id": batch_id, "cleanup_only": cleanup_only},
            created_by=None,
            priority=2,
            dedupe_key=f"analysis-batch-import:{batch_id}:{'cleanup' if cleanup_only else 'import'}",
        )
        current = self.jobs.get(job_id)
        if current is not None and str(current["status"]) == "pending":
            self.job_service.start(job_id)
        return job_id

    def _phase_is_stale(self, batch: dict[str, Any]) -> bool:
        raw = str(batch.get("phase_started_at") or batch.get("updated_at") or "")
        try:
            started = datetime.fromisoformat(raw).timestamp()
        except (TypeError, ValueError, OverflowError):
            return True
        timeout = max(60, int(self.settings.get("batch.recovery_timeout_seconds", 900)))
        return time.time() - started >= timeout

    def _mark_unknown_after_restart(self, batch: dict[str, Any], *, upload: bool) -> None:
        state = "upload_unknown" if upload else "submission_unknown"
        self.batches.update_batch(
            str(batch["id"]),
            status=state,
            remote_status=state,
            last_error_code=state,
            last_error_message="程序重啟後外部 side effect 結果未知，等待管理員驗證",
            completed_at=None,
            phase_started_at=utc_now(),
        )
        for item in self.batches.items(str(batch["id"])):
            if str(item["status"]) in {"pending", "submitted"}:
                self.batches.update_item(
                    str(item["id"]), status=state, error_code=state, error_message="restart recovery hold"
                )

    def poll_due(self, *, limit: int = 20) -> dict[str, int]:
        statuses = {
            "preparing",
            "uploading",
            "upload_unknown",
            "uploaded",
            "submitting",
            "submission_unknown",
            "validating",
            "in_progress",
            "finalizing",
            "cancelling",
            "import_pending",
            "importing",
            "cleanup_pending",
        }
        rows = self.batches.list(statuses=statuses, limit=limit)
        polled = 0
        enqueued = 0
        for batch in rows:
            batch_id = str(batch["id"])
            status = str(batch["status"])
            if status == "preparing":
                if self._phase_is_stale(batch):
                    self.batches.update_batch(
                        batch_id,
                        status="failed",
                        remote_status="failed",
                        last_error_code="BATCH-RECOVERY-PREPARING",
                        last_error_message="JSONL preparation 在重啟前未完成",
                        cleanup_status="not_required",
                    )
                    for item in self.batches.items(batch_id):
                        if str(item["status"]) == "pending":
                            self.batches.update_item(
                                str(item["id"]), status="failed", error_code="BATCH-RECOVERY-PREPARING"
                            )
                    self._finish(batch_id)
                continue
            if status == "uploading":
                if self._phase_is_stale(batch):
                    self._mark_unknown_after_restart(batch, upload=True)
                continue
            if status == "upload_unknown" or status == "submission_unknown":
                # Unknown side effects are an intentional hold.  Scheduler
                # must never call POST /files or POST /batches again.
                continue
            if status == "uploaded":
                plan_row = self.jobs.get(str(batch["job_id"])) if batch["job_id"] else None
                try:
                    plan = json.loads(str(plan_row["analysis_spec_json"] or "{}")) if plan_row else {}
                    self._submit_one(batch_id, plan)
                except Exception as exc:
                    self.batches.update_batch(
                        batch_id,
                        last_error_code=str(getattr(exc, "code", "BATCH-RECOVERY-UPLOAD-001")),
                        last_error_message=str(exc)[:1000],
                    )
                continue
            if status == "submitting":
                if self._phase_is_stale(batch):
                    self._mark_unknown_after_restart(batch, upload=False)
                continue
            if status == "validating" and not batch["remote_batch_id"]:
                self._mark_unknown_after_restart(batch, upload=False)
                continue
            if status == "cleanup_pending":
                self._enqueue_import(batch_id, cleanup_only=True)
                enqueued += 1
                continue
            if status in {"import_pending", "importing"}:
                self._enqueue_import(batch_id)
                enqueued += 1
                continue
            remote_id = str(batch["remote_batch_id"] or "")
            if not remote_id:
                continue
            plan_row = self.jobs.get(str(batch["job_id"])) if batch["job_id"] else None
            provider = None
            try:
                try:
                    plan = json.loads(str(plan_row["analysis_spec_json"] or "{}")) if plan_row else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    has_files = any(
                        batch[key] for key in ("input_file_id", "output_file_id", "error_file_id")
                    )
                    self.batches.update_batch(
                        batch_id,
                        status="cleanup_pending" if has_files else "failed",
                        remote_status="failed",
                        cleanup_status="pending" if has_files else "not_required",
                        last_error_code="BATCH-POLL-PLAN-001",
                        last_error_message="Frozen Analysis Plan JSON 無法解析；停止輪詢並進入清理",
                        completed_at=None if has_files else utc_now(),
                    )
                    if has_files:
                        self._enqueue_import(batch_id, cleanup_only=True)
                        enqueued += 1
                    else:
                        self._finish(batch_id)
                    continue
                provider = self._provider(str(batch["provider_id"]), plan)
                remote = provider.retrieve_batch(remote_id)
                state = self.batches.set_status_from_remote(batch_id, remote)
                polled += 1
                if state == "import_pending":
                    self._enqueue_import(batch_id)
                    enqueued += 1
            except Exception as exc:
                self.batches.update_batch(
                    batch_id,
                    last_error_code=str(getattr(exc, "code", "BATCH-POLL-001")),
                    last_error_message=str(exc)[:1000],
                )
            finally:
                if provider is not None and callable(getattr(provider, "close", None)):
                    provider.close()
        return {"polled": polled, "enqueued": enqueued}

    def cancel(self, batch_id: str) -> dict[str, Any]:
        batch = self.batches.get(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        if str(batch["status"]) in TERMINAL_BATCH_STATUSES:
            if str(batch["cleanup_status"]) not in {"completed", "not_required"}:
                self.batches.update_batch(batch_id, status="cleanup_pending")
                self._enqueue_import(batch_id, cleanup_only=True)
                return {"status": "cleanup_pending", "cleanup_retry": True}
            return {"status": str(batch["status"]), "already_terminal": True}
        plan_row = self.jobs.get(str(batch["job_id"])) if batch["job_id"] else None
        plan = json.loads(str(plan_row["analysis_spec_json"] or "{}")) if plan_row else {}
        if not batch["remote_batch_id"]:
            if str(batch["status"]) in {"upload_unknown", "submission_unknown", "submitting", "uploading"}:
                raise BatchLifecycleError(
                    "外部 side effect 結果未知，請使用 Confirm no remote Batch / Abandon",
                    "BATCH-CANCEL-UNKNOWN",
                )
            has_files = any(batch[key] for key in ("input_file_id", "output_file_id", "error_file_id"))
            if has_files:
                self.batches.update_batch(
                    batch_id,
                    status="cleanup_pending",
                    remote_status="cancelled",
                    last_error_code="cancel_pending_cleanup",
                    last_error_message="取消後等待本機已知的遠端檔案清理",
                    cleanup_status="pending",
                )
                self._enqueue_import(batch_id, cleanup_only=True)
                return {"status": "cleanup_pending", "local_only": True}
            self.batches.update_batch(
                batch_id,
                status="cancelled",
                remote_status="cancelled",
                completed_at=utc_now(),
                cleanup_status="not_required",
                cleanup_completed_at=utc_now(),
            )
            for item in self.batches.items(batch_id):
                if str(item["status"]) in {"pending", "submitted", "upload_unknown", "submission_unknown"}:
                    self.batches.update_item(str(item["id"]), status="cancelled", error_code="cancelled")
            self._finish(batch_id)
            return {"status": "cancelled", "local_only": True}
        provider = self._provider(str(batch["provider_id"]), plan)
        try:
            self.batches.update_batch(batch_id, status="cancelling")
            remote = provider.cancel_batch(str(batch["remote_batch_id"]))
            state = self.batches.set_status_from_remote(batch_id, remote)
            if state == "import_pending":
                self._enqueue_import(batch_id)
            return {"status": str(self.batches.get(batch_id)["status"])}
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    def abandon(self, batch_id: str, *, confirmed_no_remote: bool) -> dict[str, Any]:
        """Explicitly abandon an unknown submission after human confirmation."""

        if confirmed_no_remote is not True:
            raise BatchLifecycleError("Abandon 必須明確確認遠端 Batch 不存在", "BATCH-ABANDON-CONFIRM-001")
        batch = self.batches.get(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        if batch["remote_batch_id"]:
            raise BatchLifecycleError("已有遠端 Batch ID，不可 Abandon", "BATCH-ABANDON-REMOTE-001")
        if str(batch["status"]) not in {
            "upload_unknown",
            "submission_unknown",
            "submitting",
            "uploading",
            "uploaded",
            "validating",
        }:
            raise BatchLifecycleError("目前 Batch 不在可 Abandon 階段", "BATCH-ABANDON-002")
        has_files = any(batch[key] for key in ("input_file_id", "output_file_id", "error_file_id"))
        self.batches.update_batch(
            batch_id,
            abandon_confirmed_at=utc_now(),
            last_error_code="abandon_pending_cleanup" if has_files else "abandoned",
            last_error_message="管理員已確認遠端 Batch 不存在",
            status="cleanup_pending" if has_files else "failed",
            remote_status="abandoned",
            cleanup_status="pending" if has_files else "not_required",
            completed_at=None if has_files else utc_now(),
        )
        if has_files:
            self._enqueue_import(batch_id, cleanup_only=True)
            return {"status": "cleanup_pending", "batch_id": batch_id}
        for item in self.batches.items(batch_id):
            if str(item["status"]) not in {"imported", "failed", "cancelled"}:
                self.batches.update_item(str(item["id"]), status="failed", error_code="abandoned")
        self._finish(batch_id)
        return {"status": str(self.batches.get(batch_id)["status"]), "batch_id": batch_id}

    def retry_failed(self, batch_id: str, *, created_by: str | None = None) -> dict[str, Any]:
        items = self.batches.items(batch_id)
        retry_statuses = {
            "failed",
            "missing",
            "retry_pending",
            "schema_invalid",
            "duplicate_custom_id",
            "unexpected_custom_id",
        }
        photo_ids = [
            str(item["photo_id"])
            for item in items
            if str(item["status"]) in retry_statuses
            and str(item.get("error_code") or "") != "submission_unknown"
            and item["photo_id"]
        ]
        if not photo_ids:
            raise BatchLifecycleError("此 Batch 沒有可重試項目", "BATCH-RETRY-001")
        return self.submit(scope="manual_selection", photo_ids=photo_ids, created_by=created_by)

    def recover_submission(self, batch_id: str, remote_batch_id: str) -> dict[str, Any]:
        """Bind a manually confirmed remote Batch without creating another one.

        The remote object is fetched with the frozen Provider route first.  No
        local mutation or cleanup is allowed until every ownership assertion
        succeeds.
        """

        remote_id = str(remote_batch_id or "").strip()
        if not REMOTE_BATCH_ID_RE.fullmatch(remote_id):
            raise BatchLifecycleError("遠端 Batch ID 格式不合法", "BATCH-RECOVERY-001")
        batch = self.batches.get(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        if str(batch["status"]) not in {"submission_unknown", "submitting", "validating"}:
            raise BatchLifecycleError("目前 Batch 不在可 Recovery 的提交階段", "BATCH-RECOVERY-002")
        if str(batch["status"]) == "validating" and batch["remote_batch_id"]:
            raise BatchLifecycleError("Batch 已有遠端 ID，應由 Poll 流程處理", "BATCH-RECOVERY-002")
        plan_row = self.jobs.get(str(batch["job_id"])) if batch["job_id"] else None
        try:
            plan = json.loads(str(plan_row["analysis_spec_json"] or "{}")) if plan_row else {}
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BatchLifecycleError(
                "Frozen Analysis Plan JSON 無法解析", "BATCH-RECOVERY-PLAN-001"
            ) from exc
        provider = None
        try:
            provider = self._provider(str(batch["provider_id"]), plan)
            remote = provider.retrieve_batch(remote_id)
        finally:
            if provider is not None and callable(getattr(provider, "close", None)):
                provider.close()
        if not isinstance(remote, dict) or str(remote.get("id") or "") != remote_id:
            raise BatchLifecycleError("遠端 Batch ID 與人工輸入不一致", "BATCH-RECOVERY-OWNERSHIP-001")
        if str(remote.get("endpoint") or "") != self.ENDPOINT:
            raise BatchLifecycleError("遠端 Batch endpoint 不符合本機契約", "BATCH-RECOVERY-OWNERSHIP-002")
        if str(remote.get("input_file_id") or "") != str(batch["input_file_id"] or ""):
            raise BatchLifecycleError(
                "遠端 input_file_id 不符合本機已保存檔案", "BATCH-RECOVERY-OWNERSHIP-003"
            )
        metadata = remote.get("metadata")
        if not isinstance(metadata, dict):
            raise BatchLifecycleError("遠端 Batch 缺少 ownership metadata", "BATCH-RECOVERY-OWNERSHIP-004")
        if str(metadata.get("inktime_batch_id") or "") != batch_id:
            raise BatchLifecycleError(
                "遠端 metadata Batch ID 不符合本機 Batch", "BATCH-RECOVERY-OWNERSHIP-005"
            )
        if str(metadata.get("inktime_version") or "") != "batch-lifecycle-v1":
            raise BatchLifecycleError(
                "遠端 Batch lifecycle version 不符合本機契約", "BATCH-RECOVERY-OWNERSHIP-006"
            )
        route = next(
            (
                item
                for item in plan.get("provider_route", [])
                if str(item.get("provider_id")) == str(batch["provider_id"])
            ),
            None,
        )
        current_route = next(
            (
                item
                for item in self.provider_service.route_snapshot()
                if str(item.get("provider_id")) == str(batch["provider_id"])
            ),
            None,
        )
        if (
            not route
            or not current_route
            or str(route.get("config_revision")) != str(current_route.get("config_revision"))
        ):
            raise BatchLifecycleError(
                "Provider/project context 已變更，拒絕 Recovery", "BATCH-RECOVERY-OWNERSHIP-007"
            )
        counts = remote.get("request_counts")
        if (
            isinstance(counts, dict)
            and "total" in counts
            and int(counts.get("total") or 0) != int(batch["total_items"] or 0)
        ):
            raise BatchLifecycleError(
                "遠端 request_counts.total 與本機 Batch 數量不一致", "BATCH-RECOVERY-OWNERSHIP-008"
            )
        existing = str(batch["remote_batch_id"] or "")
        if existing and existing != remote_id:
            raise BatchLifecycleError("Batch 已綁定其他遠端 ID", "BATCH-RECOVERY-003")
        state = self.batches.bind_recovered_remote(batch_id, remote)
        self.jobs.reopen_batch_job(str(batch["job_id"])) if batch["job_id"] else None
        self._activity(
            "INFO",
            "batch_submission_recovered",
            "已綁定人工確認的既有遠端 Batch",
            batch_id=batch_id,
            status=state,
        )
        if state == "import_pending":
            self._enqueue_import(batch_id)
        return {"batch_id": batch_id, "remote_batch_id": remote_id, "status": state}

    def retry_cleanup(self, batch_id: str) -> str:
        batch = self.batches.get(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        if str(batch["cleanup_status"]) == "completed":
            return self._enqueue_import(batch_id, cleanup_only=True)
        self.batches.update_batch(batch_id, status="cleanup_pending")
        return self._enqueue_import(batch_id, cleanup_only=True)

    @staticmethod
    def _usage_from_body(body: dict[str, Any]) -> Usage:
        usage = body.get("usage") or {}
        prompt_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        return Usage(
            int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0),
            int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0),
            int(prompt_details.get("cached_tokens", 0) or 0),
            int(completion_details.get("reasoning_tokens", 0) or 0),
        )

    @staticmethod
    def _current_content_sha(photo) -> str | None:
        try:
            source = safe_join(Path(str(photo["root_path"])), str(photo["relative_path"]))
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except (OSError, ValueError):
            return None

    def _mark_item_error(
        self,
        item: dict[str, Any],
        code: str,
        message: str,
        status: str = "failed",
        *,
        job_id: str | None = None,
    ) -> None:
        if str(item["status"]) == "imported":
            return
        self.batches.update_item(
            str(item["id"]),
            status=status,
            error_code=code,
            error_message=str(message)[:1000],
        )
        if item.get("job_item_id") and job_id:
            self.jobs.fail_batch_item(str(job_id), str(item["job_item_id"]), code, str(message))

    def _import_success(
        self,
        batch: dict[str, Any],
        item: dict[str, Any],
        line: dict[str, Any],
        body: dict[str, Any],
        plan: dict[str, Any],
        provider,
    ) -> None:
        if str(item["status"]) == "imported":
            return
        photo = self.photos.get_with_path(str(item["photo_id"]))
        if photo is None:
            self._mark_item_error(
                item, "BATCH-PHOTO-MISSING", "照片已不存在", "stale", job_id=str(batch["job_id"])
            )
            return
        current_sha = (self._current_content_sha(photo) or "").casefold()
        vision_input = json.loads(str(item["vision_input_spec_json"] or "{}"))
        current_fp = fingerprint(
            {
                "content_sha256": current_sha,
                "actual_provider": str(batch["provider_id"]),
                "model": str(batch["model"]),
                "prompt_version": str(plan["prompt_version"]),
                "schema_version": SCHEMA_VERSION,
                "schema_kind": "full",
                **vision_input,
            }
        )
        if current_sha != str(item["content_sha256"]).casefold() or current_fp != str(
            item["vision_request_fingerprint"]
        ):
            self._mark_item_error(
                item,
                "BATCH-STALE",
                "送出後照片內容或 Vision Request Fingerprint 已變更",
                "stale",
                job_id=str(batch["job_id"]),
            )
            return
        response = line.get("response") or {}
        request_id = response.get("request_id") or line.get("request_id")
        usage = self._usage_from_body(body)
        raw_content = (
            body.get("choices", [{}])[0].get("message", {}).get("content") if body.get("choices") else None
        )
        if isinstance(raw_content, list):
            raw_content = "".join(str(part.get("text", "")) for part in raw_content if isinstance(part, dict))
        if not isinstance(raw_content, str) or not raw_content.strip():
            self._mark_item_error(
                item,
                "BATCH-RESPONSE-BODY",
                "HTTP 200 但沒有有效 Response Body",
                "schema_invalid",
                job_id=str(batch["job_id"]),
            )
            return
        try:
            result = validate_analysis_result(json.loads(raw_content))
        except (ValueError, TypeError, json.JSONDecodeError, AnalysisValidationError) as exc:
            self._mark_item_error(
                item, "schema_invalid", str(exc), "schema_invalid", job_id=str(batch["job_id"])
            )
            return
        result = self.analysis._apply_caption_variant(result, plan.get("caption_controls") or None)
        weights = dict(plan.get("ranking_weights") or {})
        actual_cost = float(provider.estimate_batch_cost(str(batch["model"]), usage))
        raw_line = json.dumps(line, ensure_ascii=False, separators=(",", ":"))
        with self.database.transaction(operation="analysis_batch_import") as connection:
            ranked = self.analysis._save_result(
                photo_id=str(item["photo_id"]),
                job_id=str(batch["job_id"]) if batch["job_id"] else None,
                stage="single_high",
                provider=str(batch["provider_id"]),
                model=str(batch["model"]),
                result=result,
                raw=raw_content,
                photo=photo,
                ranking_weights=weights,
                favorite_bonus=float(plan.get("favorite_bonus", 0)),
                scoring_version_id=str(plan.get("scoring_profile_id") or "") or None,
                schema_kind="full",
                prompt_version=str(plan["prompt_version"]),
                analysis_fingerprint=str(batch["analysis_fingerprint"]),
                analysis_spec_json=canonical_json(plan),
                vision_request_fingerprint=str(item["vision_request_fingerprint"]),
                vision_input_spec_json=str(item["vision_input_spec_json"]),
                travel_policy=dict(plan.get("travel_policy") or {}),
                analysis_source="analysis_batch",
                connection=connection,
            )
            self.photos.put_ai_cache(
                content_sha256=str(item["content_sha256"]),
                provider=str(batch["provider_id"]),
                model_name=str(batch["model"]),
                prompt_version=str(plan["prompt_version"]),
                schema_version=SCHEMA_VERSION,
                schema_kind="full",
                result=ranked,
                raw_json=raw_content,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
                estimated_cost=actual_cost,
                latency_ms=0,
                vision_request_fingerprint=str(item["vision_request_fingerprint"]),
                vision_input_spec_json=str(item["vision_input_spec_json"]),
                connection=connection,
            )
            self.usage.record_batch_once(
                provider=str(batch["provider_id"]),
                model=str(batch["model"]),
                job_id=str(batch["job_id"]) if batch["job_id"] else None,
                photo_id=str(item["photo_id"]),
                batch_id=str(batch["id"]),
                batch_item_id=str(item["id"]),
                request_type="analysis_batch",
                input_tokens=usage.input_tokens,
                cached_tokens=usage.cached_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                estimated_cost=actual_cost,
                actual_cost=actual_cost,
                request_id=str(request_id) if request_id else None,
                started_at=str(batch["submitted_at"] or utc_now()),
                connection=connection,
            )
            self.batches.update_item(
                str(item["id"]),
                connection=connection,
                status="imported",
                request_id=str(request_id) if request_id else None,
                http_status=int(response.get("status_code", 200) or 200),
                input_tokens=usage.input_tokens,
                cached_tokens=usage.cached_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                actual_cost=actual_cost,
                raw_response_json=raw_line,
                imported_at=utc_now(),
            )
            if item.get("job_item_id"):
                self.jobs.complete_batch_item(
                    str(batch["job_id"]),
                    str(item["job_item_id"]),
                    {"stage": "single_high", "processing_mode": "batch", "analysis": ranked},
                    actual_cost,
                    connection=connection,
                )

    def _read_results(
        self,
        path: Path | None,
        batch: dict[str, Any],
        *,
        is_error: bool,
        records: dict[str, tuple[str, dict[str, Any], dict[str, Any] | None]],
    ) -> None:
        if path is None or not path.is_file():
            return
        with path.open("r", encoding="utf-8") as stream:
            for line_number, raw_line in enumerate(stream, 1):
                if len(raw_line.encode("utf-8")) > 25 * 1024 * 1024:
                    raise BatchLifecycleError("結果 JSONL 單行超過解析上限", "BATCH-OUTPUT-LINE-001")
                try:
                    record = json.loads(raw_line)
                except (ValueError, json.JSONDecodeError):
                    self.batches.update_batch(
                        str(batch["id"]),
                        last_error_code="invalid_jsonl",
                        last_error_message=f"結果檔第 {line_number} 行不是有效 JSONL",
                    )
                    continue
                if not isinstance(record, dict):
                    self.batches.update_batch(
                        str(batch["id"]),
                        last_error_code="invalid_jsonl",
                        last_error_message=f"結果檔第 {line_number} 行不是 JSON Object",
                    )
                    continue
                custom_id = record.get("custom_id")
                if not isinstance(custom_id, str) or not CUSTOM_ID_RE.fullmatch(custom_id):
                    self.batches.update_batch(
                        str(batch["id"]),
                        last_error_code="unexpected_custom_id",
                        last_error_message=f"結果檔第 {line_number} 行含不合法 custom_id",
                    )
                    continue
                if custom_id in records:
                    records[custom_id] = ("duplicate", record, None)
                    continue
                if is_error or record.get("error") is not None:
                    records[custom_id] = ("error", record, None)
                    continue
                response = record.get("response")
                status_code = 0
                if isinstance(response, dict):
                    try:
                        status_code = int(response.get("status_code", 0) or 0)
                    except (TypeError, ValueError):
                        status_code = 0
                if not isinstance(response, dict) or status_code != 200:
                    records[custom_id] = ("error", record, None)
                    continue
                body = response.get("body")
                if not isinstance(body, dict):
                    records[custom_id] = ("schema_invalid", record, None)
                    continue
                records[custom_id] = ("success", record, body)

    def _cleanup_local(self, batch: dict[str, Any]) -> None:
        retention_days = max(0, int(self.settings.get("batch.local_retention_days", 7)))
        cutoff = time.time() - (retention_days * 24 * 60 * 60)
        for key in ("local_input_path", "local_output_path", "local_error_path"):
            raw_path = batch[key]
            if not raw_path:
                continue
            path = Path(str(raw_path))
            try:
                if path.is_file() and (retention_days == 0 or path.stat().st_mtime <= cutoff):
                    path.unlink()
            except OSError:
                # Remote cleanup is already complete; retain the path for the next
                # local housekeeping pass rather than changing the analysis result.
                continue

    def _cleanup_remote(self, batch: dict[str, Any], plan: dict[str, Any]) -> bool:
        files = (
            ("input", "input_file_id", "input_file_deleted"),
            ("output", "output_file_id", "output_file_deleted"),
            ("error", "error_file_id", "error_file_deleted"),
        )
        if not any(batch[file_key] for _, file_key, _ in files):
            self.batches.update_batch(
                batch["id"], cleanup_status="not_required", cleanup_completed_at=utc_now()
            )
            self._cleanup_local(batch)
            return True
        pending = [entry for entry in files if batch[entry[1]] and not bool(batch[entry[2]])]
        if not pending:
            self.batches.update_batch(batch["id"], cleanup_status="completed", cleanup_completed_at=utc_now())
            self._cleanup_local(batch)
            return True
        provider = self._provider(str(batch["provider_id"]), plan)
        failed = False
        try:
            for file_kind, file_key, _ in pending:
                file_id = str(batch[file_key])
                try:
                    provider.delete_remote_file(file_id)
                except Exception as exc:
                    status = int(getattr(exc, "http_status", 0) or 0)
                    code = str(getattr(exc, "code", "BATCH-CLEANUP-001"))
                    # DELETE is idempotent from the lifecycle perspective:
                    # not-found and expired remote files are already cleaned.
                    if status not in {404, 410} and not any(
                        marker in code.casefold() for marker in ("not_found", "not-found", "expired")
                    ):
                        failed = True
                        self.batches.update_batch(
                            str(batch["id"]),
                            last_error_code=code,
                            last_error_message=str(exc)[:1000],
                        )
                        continue
                self.batches.mark_file_deleted(str(batch["id"]), file_kind)
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        if failed:
            self.batches.update_batch(str(batch["id"]), status="cleanup_pending", cleanup_status="partial")
            return False
        self.batches.update_batch(
            str(batch["id"]), cleanup_status="completed", cleanup_completed_at=utc_now()
        )
        marker = str(self.batches.get(str(batch["id"]))["last_error_code"] or "")
        if marker in {"abandon_pending_cleanup", "cancel_pending_cleanup"}:
            target = "failed" if marker == "abandon_pending_cleanup" else "cancelled"
            for item in self.batches.items(str(batch["id"])):
                if str(item["status"]) not in {"imported", "failed", "cancelled"}:
                    self.batches.update_item(
                        str(item["id"]), status=target, error_code=marker, error_message=marker
                    )
            self.batches.update_batch(
                str(batch["id"]),
                status=target,
                remote_status=target,
                completed_at=utc_now(),
            )
        self._cleanup_local(batch)
        return True

    def _finish(self, batch_id: str) -> None:
        batch = self.batches.get(batch_id)
        if batch is None:
            return
        if str(batch["status"]) in {"upload_unknown", "submission_unknown"} or str(
            batch["remote_status"] or ""
        ) in {"upload_unknown", "submission_unknown"}:
            # Unknown side effects retain the item reservation and the parent
            # Job.  Only verified Recovery or explicit Abandon may leave this
            # state; never terminalize it as a local failure.
            self.batches.update_batch(batch_id, completed_at=None)
            return
        counts = self.batches.counts(batch_id)
        imported = int(counts.get("imported", 0))
        failed = sum(
            int(counts.get(status, 0))
            for status in ("failed", "schema_invalid", "duplicate_custom_id", "unexpected_custom_id")
        )
        missing = int(counts.get("missing", 0) + counts.get("retry_pending", 0))
        stale = int(counts.get("stale", 0))
        with self.database.session() as connection:
            totals = connection.execute(
                "SELECT COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(cached_tokens),0) cached_tokens,COALESCE(SUM(output_tokens),0) output_tokens,COALESCE(SUM(reasoning_tokens),0) reasoning_tokens,COALESCE(SUM(actual_cost),0) actual_cost FROM analysis_batch_items WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
        current_status = str(batch["remote_status"] or "")
        reconciliation_error = str(batch["last_error_code"] or "") in {
            "invalid_jsonl",
            "unexpected_custom_id",
        }
        final_status = (
            "completed"
            if not failed and not missing and not stale and not reconciliation_error
            else "completed_with_errors"
        )
        if current_status in {"expired", "cancelled"}:
            final_status = current_status
        if (
            current_status == "submission_unknown"
            or str(batch["last_error_code"] or "") == "submission_unknown"
        ):
            final_status = "failed"
        if current_status == "failed" and imported == 0:
            final_status = "failed"
        self.batches.update_batch(
            batch_id,
            status=final_status,
            completed_items=imported,
            failed_items=failed,
            missing_items=missing,
            stale_items=stale,
            imported_items=imported,
            input_tokens=int(totals["input_tokens"]),
            cached_tokens=int(totals["cached_tokens"]),
            output_tokens=int(totals["output_tokens"]),
            reasoning_tokens=int(totals["reasoning_tokens"]),
            actual_cost=float(totals["actual_cost"]),
            completed_at=utc_now(),
        )
        if batch["job_id"]:
            terminal_marks = ",".join("?" for _ in TERMINAL_BATCH_STATUSES)
            with self.database.session() as connection:
                pending_batches = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM analysis_batches WHERE job_id=? AND status NOT IN ({terminal_marks})",  # noqa: S608
                        (str(batch["job_id"]), *sorted(TERMINAL_BATCH_STATUSES)),
                    ).fetchone()[0]
                )
                batch_errors = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM analysis_batches WHERE job_id=? AND status!='completed'",
                        (str(batch["job_id"]),),
                    ).fetchone()[0]
                )
            if pending_batches == 0:
                self.jobs.finalize_batch_job(
                    str(batch["job_id"]),
                    status="completed" if batch_errors == 0 else "completed_with_errors",
                )

    def import_batch(self, batch_id: str, *, cleanup_only: bool = False) -> dict[str, Any]:
        batch = self.batches.get(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        if str(batch["status"]) in TERMINAL_BATCH_STATUSES and str(batch["cleanup_status"]) == "completed":
            return {"batch_id": batch_id, "already_imported": True}
        plan_row = self.jobs.get(str(batch["job_id"])) if batch["job_id"] else None
        try:
            plan = json.loads(str(plan_row["analysis_spec_json"] or "{}")) if plan_row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            has_files = any(batch[key] for key in ("input_file_id", "output_file_id", "error_file_id"))
            self.batches.update_batch(
                batch_id,
                status="cleanup_pending" if has_files else "failed",
                remote_status="failed",
                cleanup_status="pending" if has_files else "not_required",
                last_error_code="BATCH-IMPORT-PLAN-001",
                last_error_message="Frozen Analysis Plan JSON 無法解析；不匯入不完整結果",
                completed_at=None if has_files else utc_now(),
            )
            for item in self.batches.items(batch_id):
                if str(item["status"]) not in {"imported", "failed", "cancelled"}:
                    self.batches.update_item(
                        str(item["id"]),
                        status="failed",
                        error_code="BATCH-IMPORT-PLAN-001",
                        error_message="Frozen Analysis Plan JSON 無法解析",
                    )
            plan = self._cleanup_plan(batch) if has_files else {}
            cleanup_only = True
        if cleanup_only or str(batch["status"]) == "cleanup_pending":
            self._cleanup_remote(batch, plan)
            if str(self.batches.get(batch_id)["cleanup_status"]) == "completed":
                self._finish(batch_id)
            return {"batch_id": batch_id, "cleanup_only": True}
        self.batches.update_batch(batch_id, status="importing")
        provider = self._provider(str(batch["provider_id"]), plan)
        try:
            output_path = Path(
                str(batch["local_output_path"] or (self.batch_root / batch_id / "output.jsonl"))
            )
            error_path = Path(str(batch["local_error_path"] or (self.batch_root / batch_id / "error.jsonl")))
            if batch["output_file_id"] and not output_path.is_file():
                provider.download_file_content(str(batch["output_file_id"]), output_path)
                self.batches.update_batch(batch_id, local_output_path=str(output_path))
            if batch["error_file_id"] and not error_path.is_file():
                provider.download_file_content(str(batch["error_file_id"]), error_path)
                self.batches.update_batch(batch_id, local_error_path=str(error_path))
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        provider = self._provider(str(batch["provider_id"]), plan)
        records: dict[str, tuple[str, dict[str, Any], dict[str, Any] | None]] = {}
        successes: set[str] = set()
        errors: set[str] = set()
        try:
            current_batch = self.batches.get(batch_id)
            self._read_results(
                Path(str(current_batch["local_output_path"])) if current_batch["local_output_path"] else None,
                current_batch,
                is_error=False,
                records=records,
            )
            current_batch = self.batches.get(batch_id)
            self._read_results(
                Path(str(current_batch["local_error_path"])) if current_batch["local_error_path"] else None,
                current_batch,
                is_error=True,
                records=records,
            )
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        expected = {str(item["custom_id"]): item for item in self.batches.items(batch_id)}
        unknown = set(records) - set(expected)
        if unknown:
            self.batches.update_batch(
                batch_id,
                last_error_code="unexpected_custom_id",
                last_error_message=f"結果檔含 {len(unknown)} 個未預期 custom_id",
            )
        for custom_id, (kind, record, body) in records.items():
            item = expected.get(custom_id)
            if item is None:
                continue
            if kind == "duplicate":
                self._mark_item_error(
                    item,
                    "duplicate_custom_id",
                    "同一 custom_id 出現在多個結果行或成功／錯誤檔",
                    "duplicate_custom_id",
                    job_id=str(batch["job_id"]),
                )
                errors.add(custom_id)
            elif kind == "error":
                error = record.get("error") or {}
                self._mark_item_error(
                    item,
                    str(error.get("code") or "BATCH-HTTP-ERROR"),
                    str(error.get("message") or "遠端 Batch 項目失敗"),
                    "failed",
                    job_id=str(batch["job_id"]),
                )
                errors.add(custom_id)
            elif kind == "schema_invalid":
                self._mark_item_error(
                    item,
                    "BATCH-RESPONSE-BODY",
                    "Batch Response Body 不是 JSON Object",
                    "schema_invalid",
                    job_id=str(batch["job_id"]),
                )
                errors.add(custom_id)
            elif kind == "success" and body is not None:
                successes.add(custom_id)
                self._import_success(batch, item, record, body, plan, provider)
        seen = set(records)
        for custom_id, item in expected.items():
            if custom_id in seen:
                continue
            if str(item["status"]) in {
                "imported",
                "failed",
                "schema_invalid",
                "stale",
                "duplicate_custom_id",
            }:
                continue
            self.batches.update_item(
                str(item["id"]),
                status="retry_pending",
                error_code="missing_result",
                error_message="Output/Error File 未包含此 custom_id",
            )
            if item.get("job_item_id"):
                self.jobs.fail_batch_item(
                    str(batch["job_id"]),
                    str(item["job_item_id"]),
                    "missing_result",
                    "Output/Error File 未包含此 custom_id",
                )
        current = self.batches.get(batch_id)
        if current is not None:
            self._cleanup_remote(current, plan)
        current = self.batches.get(batch_id)
        if current is not None and str(current["cleanup_status"]) == "completed":
            self._finish(batch_id)
        return {
            "batch_id": batch_id,
            "success": len(successes),
            "errors": len(errors),
            "missing": len(set(expected) - seen),
        }

    def get_detail(self, batch_id: str) -> dict[str, Any] | None:
        batch = self.batches.get(batch_id)
        if batch is None:
            return None
        result = dict(batch)
        result["items_by_status"] = self.batches.counts(batch_id)
        result["items"] = self.batches.items(batch_id)
        input_path = Path(str(batch["local_input_path"])) if batch["local_input_path"] else None
        result["actual_jsonl_bytes"] = input_path.stat().st_size if input_path and input_path.is_file() else 0
        shard_rows = self.batches.list(limit=500)
        result["shard_sizes"] = []
        for shard in shard_rows:
            if str(shard.get("job_id") or "") != str(batch["job_id"] or ""):
                continue
            shard_path = Path(str(shard["local_input_path"])) if shard.get("local_input_path") else None
            result["shard_sizes"].append(
                {
                    "batch_id": str(shard["id"]),
                    "bytes": shard_path.stat().st_size if shard_path and shard_path.is_file() else 0,
                }
            )
        imported = int(batch["imported_items"] or 0)
        result["average_cost"] = float(batch["actual_cost"] or 0) / imported if imported else 0.0
        result["per_thousand_cost"] = result["average_cost"] * 1000
        result["schema_success_rate"] = (imported / int(batch["total_items"] or 1)) * 100
        with self.database.session() as connection:
            result["eligible_missing_count"] = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM photos
                    WHERE lifecycle_status='active' AND eligible=1 AND COALESCE(never_upload,0)=0
                      AND COALESCE(exclusion_status,'')!='manually_excluded'
                      AND NOT EXISTS (
                          SELECT 1 FROM photo_analysis a
                          WHERE a.photo_id=photos.id AND a.analysis_fingerprint=?
                            AND COALESCE(a.schema_kind,'basic')='full'
                      )
                    """,
                    (str(batch["analysis_fingerprint"]),),
                ).fetchone()[0]
            )
        result["full_library_estimated_cost"] = result["average_cost"] * result["eligible_missing_count"]
        return result
