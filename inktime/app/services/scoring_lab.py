from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from hashlib import file_digest
from pathlib import Path
import time
from uuid import uuid4

from inktime.app.domain.photos.formats import load_rgb
from inktime.app.core.idempotency import request_fingerprint

from inktime.app.domain.analysis.plan import analysis_output_token_cap
from inktime.app.domain.analysis.schema import AnalysisValidationError, validate_model_response
from inktime.app.domain.analysis.scoring import calculate_ranking_score
from inktime.app.services.analysis import PhotoAnalysisService
from inktime.app.domain.photos.preprocessing import PhotoPreprocessor
from inktime.app.domain.photos.quality_policy import evaluate_local_quality, local_candidate_score
from inktime.app.repositories.scoring import ScoringProfileRepository
from inktime.app.repositories.settings import SettingsRepository
from inktime.app.repositories.usage import UsageRepository
from inktime.app.repositories.billable_operations import BillableOperationRepository
from inktime.app.providers.base import VisionAttemptState, IncompleteProviderResponse, assert_complete_response
from inktime.app.services.budgets import BudgetService
from inktime.app.services.providers import ProviderService
from inktime.app.services.usage_tracking import record_failed_unknown_usage





class ScoringLabService:
    def __init__(
        self,
        providers: ProviderService,
        profiles: ScoringProfileRepository,
        settings: SettingsRepository,
        usage: UsageRepository,
        budgets: BudgetService,
        analysis: PhotoAnalysisService,
    ) -> None:
        self.providers = providers
        self.profiles = profiles
        self.settings = settings
        self.usage = usage
        self.budgets = budgets
        self.analysis = analysis

    def normalize_image(self, source: Path, destination: Path) -> None:
        plan = self._plan()
        with load_rgb(source, int(plan["vision_input"]["max_side"])) as image:
            image.save(destination, "JPEG", quality=90, optimize=True)

    def _plan(self) -> dict:
        return self.analysis.build_plan(
            strategy="single", provider_route=self.providers.usable_route_snapshot(),
            scoring_profile_id=str(self.profiles.current()["id"]),
        )

    def analyze(self, image_path: Path, *, idempotency_key: str | None = None) -> dict:
        plan = self._plan()
        provider = self.providers.build_router(
            plan["provider_route"], scoring_rules=plan["scoring_rules"], caption_controls=plan["caption_controls"],
        )
        if provider is None:
            raise ValueError("VLM-008 尚未設定可用 Provider")
        try:
            return self._analyze_with_provider(image_path, provider, plan=plan, idempotency_key=idempotency_key)
        finally:
            close_provider = getattr(provider, "close", None)
            if callable(close_provider):
                close_provider()

    def _analyze_with_provider(
        self, image_path: Path, provider, *, plan: dict | None = None, idempotency_key: str | None = None,
    ) -> dict:
        profile = self.profiles.current()
        plan = plan or self._plan()
        model = str(plan["model"])
        max_tokens = analysis_output_token_cap(self.settings)
        with image_path.open("rb") as source:
            content_sha256 = file_digest(source, "sha256").hexdigest()
        operations = BillableOperationRepository(self.usage.database)
        operation_id, checkpoint = operations.begin(
            content_sha256, idempotency_key or f"scoring-test:{uuid4()}", reuse_completed=True,
            context={"request_signature": request_fingerprint({"plan": plan, "max_tokens": max_tokens}), "kind": "scoring_test"},
        )
        provider_request_context_id = f"scoring_test|{operation_id}"
        if checkpoint is None:
            try:
                self.budgets.reserve(operation_id, self.budgets.estimate_reserve(provider, model, output_tokens=max_tokens))
            except Exception:
                operations.finish(operation_id, not_sent=True)
                raise
        selected_provider = provider
        provider_id = ""
        provider_name = provider.name
        attempt_summary: list[dict] = []
        vision_request_state = VisionAttemptState()

        def refresh_provider_identity() -> None:
            nonlocal selected_provider, provider_id, provider_name, model
            selected = getattr(getattr(provider, "_local", None), "channel", None)
            selected_provider = selected.provider if selected is not None else provider
            provider_id = str(getattr(selected_provider, "provider_id", selected_provider.name))
            provider_name = str(getattr(selected_provider, "name", provider.name))
            configured_model = str(getattr(selected, "model", "") or "").strip()
            if configured_model:
                model = configured_model

        def provider_request_metrics() -> dict:
            return dict(getattr(selected_provider, "last_request_metrics", {}) or {})

        def record_attempt(
            response,
            *,
            request_type: str,
            started_at: str,
            started_perf: float,
            retry_count: int,
            image_bytes: bool,
        ) -> dict:
            usage = response.usage
            recorded_model = str(response.served_model or model)
            saved = (response.request_metrics or {}).get("scoring_checkpoint", {})
            estimated = saved.get("estimated_cost") if saved else (
                provider.estimate_cost(recorded_model, usage) if usage.tokens_reported else None
            )
            latency_ms = int(saved.get("latency_ms", (time.perf_counter() - started_perf) * 1000))
            reported = usage.provider_reported_cost
            source = "provider_reported" if reported is not None else "estimated" if estimated is not None else "unknown"
            metrics = dict(response.request_metrics or getattr(selected_provider, "last_request_metrics", {}) or {})
            if image_bytes:
                # Some compatible/fake providers do not expose transport
                # metrics.  The Vision attempt still carried the image, so
                # preserve a positive local byte measurement rather than
                # silently recording it as a text-only request.
                reported_image_bytes = metrics.get("image_bytes", 0)
                try:
                    reported_image_bytes = int(reported_image_bytes)
                except (TypeError, ValueError):
                    reported_image_bytes = 0
                metrics["image_bytes"] = (
                    reported_image_bytes
                    if reported_image_bytes > 0
                    else image_path.stat().st_size
                )
            else:
                metrics["image_bytes"] = 0
            effective = (
                max(0.0, float(reported))
                if reported is not None
                else max(0.0, float(estimated))
                if estimated is not None
                else 0.0
            )
            self.usage.record(
                provider=provider_name,
                provider_id=provider_id,
                model=recorded_model,
                job_id=None,
                photo_id=None,
                request_type=request_type,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
                estimated_cost=estimated,
                actual_cost=max(0.0, float(reported)) if reported is not None else None,
                started_at=started_at,
                latency_ms=latency_ms,
                status="completed",
                retry_count=retry_count,
                request_id=response.request_id,
                reasoning_tokens=usage.reasoning_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cost_source=source,
                prompt_chars=metrics.get("prompt_chars", 0),
                schema_chars=metrics.get("schema_chars", 0),
                request_body_bytes=metrics.get("request_body_bytes", 0),
                image_bytes=metrics.get("image_bytes", 0),
                operation_id=operation_id,
                tokens_reported=usage.tokens_reported,
            )
            result = {
                "request_type": request_type,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_tokens": usage.cached_tokens,
                "estimated_cost": estimated,
                "actual_cost": reported,
                "effective_cost": effective,
                "cost_source": source,
                "latency_ms": latency_ms,
                "image_bytes": metrics.get("image_bytes", 0),
            }
            attempt_summary.append(result)
            return result

        def record_failed_attempt(
            *,
            request_type: str,
            model_name: str,
            started_at: str,
            started_perf: float,
            retry_count: int,
            image_request: bool,
            error: Exception,
        ) -> None:
            refresh_provider_identity()
            metrics = provider_request_metrics()
            if image_request:
                try:
                    reported_image_bytes = int(metrics.get("image_bytes", 0))
                except (TypeError, ValueError):
                    reported_image_bytes = 0
                metrics["image_bytes"] = (
                    reported_image_bytes
                    if reported_image_bytes > 0
                    else image_path.stat().st_size
                )
            else:
                metrics["image_bytes"] = 0
            error_code = str(getattr(error, "code", "") or error.__class__.__name__)
            record_failed_unknown_usage(
                self.usage,
                provider=selected_provider,
                model=model_name,
                job_id=None,
                photo_id=None,
                request_type=request_type,
                started_at=started_at,
                started_perf=started_perf,
                error=error,
                request_metrics=metrics,
                retry_count=retry_count,
                image_bytes=metrics.get("image_bytes", 0),
                error_code=error_code,
            )
            attempt_summary.append(
                {
                    "request_type": request_type,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                    "estimated_cost": None,
                    "actual_cost": None,
                    "effective_cost": 0.0,
                    "cost_source": "unknown",
                    "latency_ms": int((time.perf_counter() - started_perf) * 1000),
                    "image_bytes": metrics.get("image_bytes", 0),
                    "status": "failed",
                    "error_code": error_code,
                }
            )

        vision_started_at = datetime.now(timezone.utc).isoformat()
        vision_started_perf = time.perf_counter()
        try:
            response = checkpoint or provider.analyze(
                image_path=image_path,
                model=model,
                detail=str(plan["vision_input"]["detail"]),
                stage="single",
                max_tokens=max_tokens,
                caption_controls=plan["caption_controls"],
                reasoning_effort=plan["reasoning_effort"],
                prompt_version=plan["prompt_version"],
                vision_attempt=vision_request_state,
                provider_request_context_id=provider_request_context_id,
            )
        except Exception as error:
            refresh_provider_identity()
            request_started = bool(
                vision_request_state.vision_started
                or getattr(error, "vision_started", False)
                or getattr(error, "request_started", False)
                or getattr(error, "ambiguous", False)
            )
            if request_started:
                record_failed_attempt(
                    request_type="scoring_test_vision",
                    model_name=model,
                    started_at=vision_started_at,
                    started_perf=vision_started_perf,
                    retry_count=0,
                    image_request=True,
                    error=error,
                )
            else:
                operations.finish(operation_id, not_sent=True)
                self.budgets.release(operation_id)
            raise
        refresh_provider_identity()
        if checkpoint is None:
            # Persist the paid response before validation or usage projection.
            # A crash at either later step must never authorize another POST.
            response = replace(response, request_metrics={
                **(response.request_metrics or {}),
                "scoring_checkpoint": {
                    "provider_id": provider_id, "provider_name": provider_name, "model": model,
                    "started_at": vision_started_at,
                    "latency_ms": int((time.perf_counter() - vision_started_perf) * 1000),
                    "estimated_cost": provider.estimate_cost(str(response.served_model or model), response.usage)
                    if response.usage.tokens_reported else None,
                },
            })
            operations.save_response(operation_id, response)
        else:
            saved = (response.request_metrics or {}).get("scoring_checkpoint", {})
            provider_id = str(saved.get("provider_id", provider_id))
            provider_name = str(saved.get("provider_name", provider_name))
            model = str(saved.get("model", model))
            vision_started_at = str(saved.get("started_at", vision_started_at))
        vision_summary = record_attempt(
            response,
            request_type="scoring_test_vision",
            started_at=vision_started_at,
            started_perf=vision_started_perf,
            retry_count=0,
            image_bytes=True,
        )
        if vision_summary["cost_source"] != "unknown":
            self.budgets.release(operation_id)
        operations.finish(operation_id)
        try:
            assert_complete_response(response)
        except IncompleteProviderResponse as error:
            raise AnalysisValidationError(str(error)) from error
        result = validate_model_response(response.content, caption_controls=plan["caption_controls"])
        total_cost = sum(float(item["effective_cost"]) for item in attempt_summary)
        unknown_count = sum(item["cost_source"] == "unknown" for item in attempt_summary)
        cost_source = (
            "incomplete"
            if unknown_count
            else "provider_reported"
            if all(item["cost_source"] == "provider_reported" for item in attempt_summary)
            else "estimated"
        )
        total_input = sum(int(item["input_tokens"]) for item in attempt_summary)
        total_output = sum(int(item["output_tokens"]) for item in attempt_summary)
        total_cached = sum(int(item["cached_tokens"]) for item in attempt_summary)
        features = PhotoPreprocessor().analyze(image_path).as_dict()
        result["local_quality_score"] = local_candidate_score(features, evaluation=evaluate_local_quality(features))
        return {
            "analysis": result,
            "ranking_score": calculate_ranking_score(
                result,
                favorite=False,
            ),
            "profile": {"id": profile["id"], "name": profile["name"]},
            "usage": {
                "input_tokens": total_input,
                "output_tokens": total_output,
                "cached_tokens": total_cached,
                "cost": total_cost,
                "cost_source": cost_source,
                "cost_complete": unknown_count == 0,
                "unknown_cost_count": unknown_count,
                "vision_cost": vision_summary["effective_cost"],
                "repair_cost": next(
                    (item["effective_cost"] for item in attempt_summary if item["request_type"] == "scoring_test_repair"),
                    0.0,
                ),
                "attempts": attempt_summary,
                "latency_ms": sum(int(item["latency_ms"]) for item in attempt_summary),
                "provider": provider_name,
                "provider_id": provider_id,
                "model": model,
            },
        }
