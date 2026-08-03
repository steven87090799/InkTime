"""Bounded model benchmark with an offline-by-default contract boundary.

The offline path builds the exact image request body but never invokes Provider
transport. It therefore measures prompt/schema/request size and keeps benchmark output
separate from photo analysis, releases, display history, and production cache.
The live path is deliberately explicit and has request and cost stops.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import tempfile
import time
from collections.abc import Iterable, Mapping
from typing import Any

from PIL import Image, ImageDraw

from inktime.app.domain.analysis.plan import normalize_reasoning_effort
from inktime.app.domain.analysis.schema import AnalysisValidationError, validate_analysis_result
from inktime.app.services.analysis import (
    CAPTION_VARIANTS_TOKEN_CAP,
    FULL_ANALYSIS_TOKEN_CAP,
    REPAIR_TOKEN_CAP,
)
from inktime.app.providers.base import ProviderResponse, Usage, VisionAttemptState
from inktime.app.providers.config import normalize_options
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider, ProviderHTTPError


MAX_SAMPLE_COUNT = 100
MAX_REQUESTS = 100
ALLOWED_SIDES = (512, 1024, 1600)


class BenchmarkError(ValueError):
    """A user-correctable benchmark configuration or safety failure."""


@dataclass(frozen=True)
class BenchmarkAxis:
    provider: str
    model: str
    image_max_side: int
    prompt_profile: str
    variants_enabled: bool
    reasoning_effort: str
    options: dict[str, Any]

    @property
    def label(self) -> str:
        return (
            f"{self.provider}/{self.model} | {self.image_max_side}px | "
            f"{self.prompt_profile} | variants={'on' if self.variants_enabled else 'off'} | "
            f"reasoning={self.reasoning_effort}"
        )


def _caption_controls(variants_enabled: bool) -> dict[str, Any]:
    return {
        "caption_min_chars": 120,
        "caption_target_chars": 160,
        "caption_max_chars": 220,
        "side_caption_min_chars": 8,
        "side_caption_target_chars": 12,
        "side_caption_max_chars": 16,
        "copy_humor_level": 1,
        "copy_poetic_level": 1,
        "copy_avoid_cliche": True,
        "copy_avoid_direct_description": True,
        "copy_forbid_exclamation": True,
        "copy_forbid_like_phrase": True,
        "copy_max_commas": 2,
        "copy_avoid_abstract_ending": True,
        "copy_banned_words": [],
        "copy_banned_patterns": [],
        "copy_custom_rules": "",
        "caption_variants_enabled": variants_enabled,
    }


def _synthetic_images(directory: Path, count: int, seed: str) -> list[Path]:
    """Create deterministic, non-personal RGB fixtures for bounded runs."""

    paths: list[Path] = []
    seed_value = sum(ord(char) for char in seed) or 1
    for index in range(count):
        width, height = 1600, 900
        base = (
            (seed_value + index * 17) % 220 + 20,
            (seed_value * 3 + index * 29) % 220 + 20,
            (seed_value * 7 + index * 41) % 220 + 20,
        )
        image = Image.new("RGB", (width, height), base)
        draw = ImageDraw.Draw(image)
        margin = 80 + (index % 5) * 20
        draw.rectangle((margin, margin, width - margin, height - margin), outline=(20, 20, 20), width=12)
        draw.ellipse((width // 3, height // 4, width * 2 // 3, height * 3 // 4), fill=(240, 240, 240))
        path = directory / f"synthetic-{index:04d}.jpg"
        image.save(path, format="JPEG", quality=88, optimize=False)
        paths.append(path)
    return paths


def _record_value(record: Mapping[str, Any] | Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if isinstance(record, Mapping) and key in record:
            return record[key]
        if hasattr(record, key):
            return getattr(record, key)
    return default


def _is_false_flag(value: Any) -> bool:
    """Accept the bool/int/text forms commonly returned by SQLite adapters."""

    if value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    return isinstance(value, str) and value.strip().casefold() in {"0", "false", "no", "inactive"}


def _exclusion_category(record: Mapping[str, Any] | Any) -> str | None:
    """Return one deterministic exclusion reason for a read-only dataset row."""

    if bool(_record_value(record, "never_upload", "privacy_never_upload", default=False)):
        return "never_upload"
    if bool(_record_value(record, "manually_excluded", "manual_exclude", "exclude_from_benchmark", default=False)):
        return "manually_excluded"
    if bool(_record_value(record, "missing", default=False)) or str(
        _record_value(record, "status", default="")
    ).casefold() in {"missing", "deleted"}:
        return "missing"
    status = str(_record_value(record, "status", default="")).casefold()
    if (
        bool(_record_value(record, "inactive", default=False))
        or _is_false_flag(_record_value(record, "active", default=True))
        or status == "inactive"
    ):
        return "inactive"
    if (
        bool(_record_value(record, "ineligible", default=False))
        or _is_false_flag(_record_value(record, "eligible", "ai_eligible", default=True))
        or status == "ineligible"
    ):
        return "ineligible"
    return None


def _select_benchmark_records(
    records: Iterable[Mapping[str, Any] | Any], *, sample_count: int, seed: str
) -> tuple[list[Mapping[str, Any] | Any], dict[str, int]]:
    if sample_count < 1 or sample_count > MAX_SAMPLE_COUNT:
        raise BenchmarkError(f"sample-count 必須介於 1 到 {MAX_SAMPLE_COUNT}")
    excluded = {name: 0 for name in ("never_upload", "inactive", "ineligible", "missing", "manually_excluded")}
    eligible: list[tuple[str, Mapping[str, Any] | Any]] = []
    for index, record in enumerate(records):
        category = _exclusion_category(record)
        if category is not None:
            excluded[category] += 1
            continue
        photo_id = str(_record_value(record, "photo_id", "id", default=f"row-{index}"))
        eligible.append((photo_id, record))
    eligible.sort(key=lambda item: (hashlib.sha256(f"{seed}{item[0]}".encode("utf-8")).hexdigest(), item[0]))
    return [record for _photo_id, record in eligible[:sample_count]], excluded


def select_benchmark_samples(
    records: Iterable[Mapping[str, Any] | Any], *, sample_count: int, seed: str
) -> list[Mapping[str, Any] | Any]:
    """Select a deterministic, privacy-filtered sample without touching production state."""

    selected, _excluded = _select_benchmark_records(records, sample_count=sample_count, seed=seed)
    return selected


def _synthetic_records(directory: Path, count: int, seed: str) -> list[dict[str, Any]]:
    return [
        {
            "photo_id": f"synthetic-{index:04d}",
            "path": str(path),
            "never_upload": False,
            "active": True,
            "eligible": True,
            "missing": False,
            "manually_excluded": False,
        }
        for index, path in enumerate(_synthetic_images(directory, count, seed))
    ]


def _resize_fixture_images(images: Iterable[Path], directory: Path, max_side: int) -> list[Path]:
    """Materialize one bounded axis at its real maximum side; never upscale."""

    directory.mkdir(parents=True, exist_ok=True)
    resized: list[Path] = []
    for index, source in enumerate(images):
        target = directory / f"fixture-{index:04d}.jpg"
        with Image.open(source) as original:
            image = original.convert("RGB")
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            image.save(target, format="JPEG", quality=88, optimize=False)
        resized.append(target)
    return resized


def _percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 3)


def _average(values: Iterable[float]) -> float | None:
    values = list(values)
    return round(sum(values) / len(values), 3) if values else None


def _new_metrics() -> dict[str, Any]:
    return {
        "total_photos": 0,
        "provider_requests": 0,
        "vision_requests": 0,
        "repair_requests": 0,
        "success_count": 0,
        "schema_success_rate": None,
        "first_pass_schema_success_rate": None,
        "repair_rate": None,
        "failure_rate": None,
        "input_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "uncached_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "estimated_cost": None,
        "provider_reported_cost": None,
        "actual_cost": None,
        "unknown_cost_count": 0,
        "avg_cost_per_photo": None,
        "cost_per_1000_photos": None,
        "avg_latency_ms": None,
        "p50_latency_ms": None,
        "p95_latency_ms": None,
        "avg_request_body_bytes": None,
        "avg_image_bytes": None,
        "avg_system_prompt_chars": None,
        "avg_schema_chars": None,
        "offline_contract_only": False,
    }


class ModelBenchmarkService:
    """Run a benchmark without mutating production domain repositories."""

    def __init__(self, *, provider_factory=OpenAICompatibleProvider) -> None:
        self.provider_factory = provider_factory

    @staticmethod
    def build_axes(
        *,
        provider: str,
        models: Iterable[str],
        image_sides: Iterable[int],
        prompt_profiles: Iterable[str],
        variants: Iterable[bool],
        reasoning_efforts: Iterable[str],
        options: dict[str, Any] | None = None,
    ) -> list[BenchmarkAxis]:
        axes: list[BenchmarkAxis] = []
        normalized_options = dict(options or {})
        for model in models:
            for side in image_sides:
                if int(side) not in ALLOWED_SIDES:
                    raise BenchmarkError(f"image side 只允許：{','.join(map(str, ALLOWED_SIDES))}")
                for profile in prompt_profiles:
                    if profile not in {"default", "advanced"}:
                        raise BenchmarkError("prompt profile 只允許 default 或 advanced")
                    for variants_enabled in variants:
                        for effort in reasoning_efforts:
                            axes.append(
                                BenchmarkAxis(
                                    provider=str(provider),
                                    model=str(model),
                                    image_max_side=int(side),
                                    prompt_profile=str(profile),
                                    variants_enabled=bool(variants_enabled),
                                    reasoning_effort=normalize_reasoning_effort(effort),
                                    options=dict(normalized_options),
                                )
                            )
        if not axes or len(axes) > 96:
            raise BenchmarkError("Benchmark axes 必須介於 1 到 96 組")
        return axes

    def _provider(self, axis: BenchmarkAxis, *, api_key: str = "", base_url: str = "https://openrouter.ai/api/v1"):
        kind = "openrouter" if axis.provider.casefold() == "openrouter" else "openai_compatible"
        options = normalize_options(kind, axis.options)
        return self.provider_factory(
            name=f"benchmark-{axis.provider}",
            base_url=base_url,
            api_key=api_key,
            kind=kind,
            provider_id=axis.provider,
            options=options,
            timeout=120,
            supports_json_schema=True,
            caption_controls=(
                _caption_controls(axis.variants_enabled) if axis.prompt_profile == "advanced" else None
            ),
            supports_reasoning_effort=kind == "openai",
        )

    @staticmethod
    def _accumulate_usage(
        metrics: dict[str, Any], provider: OpenAICompatibleProvider, model: str, response: ProviderResponse
    ) -> tuple[float | None, bool]:
        usage = response.usage
        metrics["input_tokens"] += usage.input_tokens
        metrics["cached_tokens"] += usage.cached_tokens
        metrics["cache_write_tokens"] += usage.cache_write_tokens
        metrics["uncached_tokens"] += max(0, usage.input_tokens - usage.cached_tokens)
        metrics["output_tokens"] += usage.output_tokens
        metrics["reasoning_tokens"] += usage.reasoning_tokens
        if usage.provider_reported_cost is not None:
            cost = max(0.0, float(usage.provider_reported_cost))
            metrics["provider_reported_cost"] = float(metrics["provider_reported_cost"] or 0) + cost
            return cost, True
        estimated = provider.estimate_cost(model, usage)
        if estimated is not None:
            cost = max(0.0, float(estimated))
            metrics["estimated_cost"] = float(metrics["estimated_cost"] or 0) + cost
            return cost, True
        metrics["unknown_cost_count"] += 1
        return None, False

    def run_offline(self, *, axes: list[BenchmarkAxis], sample_count: int, seed: str) -> dict[str, Any]:
        if sample_count < 1 or sample_count > MAX_SAMPLE_COUNT:
            raise BenchmarkError(f"sample-count 必須介於 1 到 {MAX_SAMPLE_COUNT}")
        report: dict[str, Any] = {
            "benchmark_version": "model-benchmark-v1",
            "mode": "offline",
            "seed": seed,
            "sample_count": sample_count,
            "selected_count": 0,
            "dataset_source": "synthetic-generated-no-private-photos",
            "excluded_counts": {},
            "axes": [],
            "stopped_by_budget": False,
            "network_invocations": 0,
            "production_mutations": 0,
        }
        with tempfile.TemporaryDirectory(prefix="inktime-benchmark-") as directory:
            records = _synthetic_records(Path(directory), sample_count, seed)
            selected, excluded = _select_benchmark_records(records, sample_count=sample_count, seed=seed)
            images = [Path(str(record["path"])) for record in selected]
            report["selected_count"] = len(images)
            report["excluded_counts"] = excluded
            for axis in axes:
                provider = self._provider(axis)
                axis_images = _resize_fixture_images(
                    images, Path(directory) / f"offline-{len(report['axes']):03d}", axis.image_max_side
                )
                metrics = _new_metrics()
                metrics["offline_contract_only"] = True
                request_metrics: list[dict[str, int]] = []
                try:
                    for image in axis_images:
                        provider.build_analysis_request_body(
                            image_path=image,
                            model=axis.model,
                            detail="high",
                            stage="single",
                            max_tokens=(
                                CAPTION_VARIANTS_TOKEN_CAP
                                if axis.variants_enabled
                                else FULL_ANALYSIS_TOKEN_CAP
                            ),
                            caption_controls=(
                                _caption_controls(axis.variants_enabled)
                                if axis.prompt_profile == "advanced"
                                else None
                            ),
                            reasoning_effort=axis.reasoning_effort,
                        )
                        request_metrics.append(dict(provider.last_request_metrics))
                finally:
                    provider.close()
                metrics["total_photos"] = len(axis_images)
                metrics["avg_request_body_bytes"] = _average(item.get("request_body_bytes", 0) for item in request_metrics)
                metrics["avg_image_bytes"] = _average(item.get("image_bytes", 0) for item in request_metrics)
                metrics["avg_system_prompt_chars"] = _average(item.get("prompt_chars", 0) for item in request_metrics)
                metrics["avg_schema_chars"] = _average(item.get("schema_chars", 0) for item in request_metrics)
                report["axes"].append({"axis": axis.label, "metrics": metrics})
        return report

    def run_live(
        self,
        *,
        axes: list[BenchmarkAxis],
        sample_count: int,
        seed: str,
        api_key: str,
        base_url: str,
        max_requests: int,
        max_cost: float,
    ) -> dict[str, Any]:
        if not api_key.strip():
            raise BenchmarkError("live benchmark 必須明確提供 API Key")
        if max_requests < 1 or max_requests > MAX_REQUESTS:
            raise BenchmarkError(f"max-requests 必須介於 1 到 {MAX_REQUESTS}")
        if max_cost <= 0:
            raise BenchmarkError("max-cost 必須是正數")
        if sample_count < 1 or sample_count > MAX_SAMPLE_COUNT:
            raise BenchmarkError(f"sample-count 必須介於 1 到 {MAX_SAMPLE_COUNT}")
        report = {
            "benchmark_version": "model-benchmark-v1",
            "mode": "live",
            "seed": seed,
            "sample_count": sample_count,
            "selected_count": 0,
            "dataset_source": "synthetic-generated-no-private-photos",
            "excluded_counts": {},
            "axes": [],
            "stopped_by_budget": False,
            "network_invocations": 0,
            "production_mutations": 0,
        }
        with tempfile.TemporaryDirectory(prefix="inktime-benchmark-live-") as directory:
            records = _synthetic_records(Path(directory), sample_count, seed)
            selected, excluded = _select_benchmark_records(records, sample_count=sample_count, seed=seed)
            images = [Path(str(record["path"])) for record in selected]
            report["selected_count"] = len(images)
            report["excluded_counts"] = excluded
            requests_used = 0
            spent = 0.0
            for axis in axes:
                metrics = _new_metrics()
                axis_images = _resize_fixture_images(
                    images, Path(directory) / f"live-{len(report['axes']):03d}", axis.image_max_side
                )
                metrics["total_photos"] = len(axis_images)
                latencies: list[float] = []
                request_sizes: list[float] = []
                image_sizes: list[float] = []
                prompt_sizes: list[float] = []
                schema_sizes: list[float] = []
                first_pass_success = 0
                repairs = 0
                provider = self._provider(axis, api_key=api_key, base_url=base_url)
                try:
                    if requests_used >= max_requests:
                        report["stopped_by_budget"] = True
                        continue
                    validate_config = getattr(provider, "validate_config", None)
                    if not callable(validate_config):
                        raise BenchmarkError("live benchmark Provider 必須先提供 /models capability check")
                    try:
                        validation = validate_config()
                    except Exception as exc:
                        requests_used += 1
                        report["network_invocations"] += 1
                        raise BenchmarkError(f"Provider /models capability check 失敗：{exc.__class__.__name__}") from exc
                    requests_used += 1
                    report["network_invocations"] += 1
                    if isinstance(validation, tuple):
                        valid, message = bool(validation[0]), str(validation[1] if len(validation) > 1 else "")
                    else:
                        valid, message = bool(validation), ""
                    if not valid:
                        raise BenchmarkError(f"Provider /models capability check 失敗：{message or 'unknown'}")
                    for image in axis_images:
                        if requests_used >= max_requests or spent >= max_cost or report["stopped_by_budget"]:
                            report["stopped_by_budget"] = True
                            break
                        started = time.perf_counter()
                        response: ProviderResponse | None = None
                        request_metrics_for_photo: list[dict[str, int]] = []
                        try:
                            response = provider.analyze(
                                image_path=image,
                                model=axis.model,
                                detail="high",
                                stage="single",
                                max_tokens=3072 if axis.variants_enabled else 2048,
                                caption_controls=(
                                    _caption_controls(axis.variants_enabled)
                                    if axis.prompt_profile == "advanced"
                                    else None
                                ),
                                reasoning_effort=axis.reasoning_effort,
                                vision_attempt=VisionAttemptState(),
                            )
                            requests_used += 1
                            report["network_invocations"] += 1
                            metrics["provider_requests"] += 1
                            metrics["vision_requests"] += 1
                            request_metrics_for_photo.append(dict(response.request_metrics or provider.last_request_metrics or {}))
                            vision_cost, vision_cost_known = self._accumulate_usage(metrics, provider, axis.model, response)
                            if vision_cost is not None:
                                spent += vision_cost
                            if not vision_cost_known:
                                report["stopped_by_budget"] = True
                            try:
                                validate_analysis_result(response.content)
                                metrics["success_count"] += 1
                                first_pass_success += 1
                            except AnalysisValidationError:
                                if requests_used >= max_requests or spent >= max_cost or report["stopped_by_budget"]:
                                    report["stopped_by_budget"] = True
                                else:
                                    repairs += 1
                                    metrics["repair_requests"] += 1
                                    repair_response = provider.repair_json(
                                        invalid_content=response.content,
                                        validation_error="benchmark schema validation",
                                        model=axis.model,
                                        max_tokens=REPAIR_TOKEN_CAP,
                                        stage="single",
                                        caption_controls=(
                                            _caption_controls(axis.variants_enabled)
                                            if axis.prompt_profile == "advanced"
                                            else None
                                        ),
                                    )
                                    requests_used += 1
                                    report["network_invocations"] += 1
                                    metrics["provider_requests"] += 1
                                    request_metrics_for_photo.append(
                                        dict(repair_response.request_metrics or provider.last_request_metrics or {})
                                    )
                                    repair_cost, repair_cost_known = self._accumulate_usage(
                                        metrics, provider, axis.model, repair_response
                                    )
                                    if repair_cost is not None:
                                        spent += repair_cost
                                    if not repair_cost_known:
                                        report["stopped_by_budget"] = True
                                    try:
                                        validate_analysis_result(repair_response.content)
                                        metrics["success_count"] += 1
                                    except AnalysisValidationError:
                                        pass
                        except (ProviderHTTPError, OSError, ValueError):
                            requests_used += 1
                            report["network_invocations"] += 1
                            metrics["provider_requests"] += 1
                        finally:
                            elapsed_ms = (time.perf_counter() - started) * 1000
                            latencies.append(elapsed_ms)
                            for used_metrics in request_metrics_for_photo or [provider.last_request_metrics or {}]:
                                request_sizes.append(float(used_metrics.get("request_body_bytes", 0)))
                                image_sizes.append(float(used_metrics.get("image_bytes", 0)))
                                prompt_sizes.append(float(used_metrics.get("prompt_chars", 0)))
                                schema_sizes.append(float(used_metrics.get("schema_chars", 0)))
                finally:
                    provider.close()
                metrics["first_pass_schema_success_rate"] = round(first_pass_success / max(1, metrics["vision_requests"]), 4)
                metrics["schema_success_rate"] = round(metrics["success_count"] / max(1, metrics["vision_requests"]), 4)
                metrics["repair_rate"] = round(repairs / max(1, metrics["vision_requests"]), 4)
                metrics["failure_rate"] = round(max(0, metrics["vision_requests"] - metrics["success_count"]) / max(1, metrics["vision_requests"]), 4)
                metrics["actual_cost"] = metrics["provider_reported_cost"] if metrics["provider_reported_cost"] is not None else metrics["estimated_cost"]
                metrics["avg_cost_per_photo"] = (
                    round(float(metrics["actual_cost"]) / metrics["total_photos"], 6)
                    if metrics["actual_cost"] is not None and metrics["total_photos"]
                    else None
                )
                metrics["cost_per_1000_photos"] = (
                    round(float(metrics["avg_cost_per_photo"]) * 1000, 3)
                    if metrics["avg_cost_per_photo"] is not None
                    else None
                )
                metrics["avg_latency_ms"] = _average(latencies)
                metrics["p50_latency_ms"] = _percentile(latencies, 0.50)
                metrics["p95_latency_ms"] = _percentile(latencies, 0.95)
                metrics["avg_request_body_bytes"] = _average(request_sizes)
                metrics["avg_image_bytes"] = _average(image_sizes)
                metrics["avg_system_prompt_chars"] = _average(prompt_sizes)
                metrics["avg_schema_chars"] = _average(schema_sizes)
                report["axes"].append({"axis": axis.label, "metrics": metrics})
                if report["stopped_by_budget"]:
                    break
        return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# InkTime Model Benchmark",
        "",
        f"- Mode: `{report['mode']}`",
        f"- Seed: `{report['seed']}`",
        f"- Dataset: `{report['dataset_source']}`",
        f"- Network invocations: `{report['network_invocations']}`",
        f"- Production mutations: `{report['production_mutations']}`",
        f"- Stopped by budget: `{report['stopped_by_budget']}`",
        "",
        "Offline reports are request-contract measurements only; they are not model quality or accuracy claims.",
        "",
        "| Axis | Photos | Provider requests | Success | Schema rate | Avg body bytes | Avg image bytes | Avg latency ms | Unknown cost |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report["axes"]:
        metrics = item["metrics"]
        lines.append(
            "| {axis} | {total_photos} | {provider_requests} | {success_count} | {schema_success_rate} | "
            "{avg_request_body_bytes} | {avg_image_bytes} | {avg_latency_ms} | {unknown_cost_count} |".format(
                axis=item["axis"], **metrics
            )
        )
    lines.extend(
        [
            "",
            "The benchmark does not write `photo_analysis`, `releases`, `display_history`, or production AI cache.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(report: dict[str, Any], *, output: Path, markdown_output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_output.write_text(markdown_report(report), encoding="utf-8")
