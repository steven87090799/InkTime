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
import math
from pathlib import Path
import tempfile
import time
from collections.abc import Iterable, Mapping
from typing import Any

from PIL import Image, ImageDraw
from inktime.app.domain.photos.formats import load_rgb

from inktime.app.domain.analysis import REPAIR_TOKEN_CAP
from inktime.app.domain.photos.preprocessing import PhotoPreprocessor
from inktime.app.domain.photos.quality_policy import local_candidate_score, evaluate_local_quality
from inktime.app.domain.analysis.content_filter import evaluate_content_filter
from inktime.app.domain.analysis.plan import normalize_reasoning_effort
from inktime.app.domain.analysis.schema import AnalysisValidationError, validate_analysis_result
from inktime.app.domain.analysis.scoring import (
    DEFAULT_FAVORITE_BONUS,
    DEFAULT_RANKING_WEIGHTS,
    RANKING_RULE_VERSION,
    calculate_ranking_score,
)
from inktime.app.services.analysis import (
    CAPTION_VARIANTS_TOKEN_CAP,
    FULL_ANALYSIS_TOKEN_CAP,
)
from inktime.app.services.benchmark_metrics import calculate_benchmark_metrics
from inktime.app.providers.base import ProviderResponse, VisionAttemptState
from inktime.app.providers.config import normalize_options
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider, ProviderHTTPError


MAX_SAMPLE_COUNT = 100
MAX_REQUESTS = 100
ALLOWED_SIDES = (512, 1024, 1600)
_MANIFEST_KEYS = {"version", "dataset", "items"}
_MANIFEST_DATASET_KEYS = {"id", "source", "privacy"}
_MANIFEST_ITEM_KEYS = {
    "id",
    "image",
    "expected",
    "expected_rank",
    "expected_score",
    "never_upload",
    "inactive",
    "ineligible",
    "missing",
    "manually_excluded",
}
_MANIFEST_EXPECTED_KEYS = {"memory_score", "visual_score", "special_level", "types", "should_keep", "rotation_cw", "ambiguous", "visual_orientation"}
_MANIFEST_ROTATIONS = {0, 90, 180, 270, None}
_BENCHMARK_RANKING_WEIGHTS = dict(DEFAULT_RANKING_WEIGHTS)


class BenchmarkError(ValueError):
    """A user-correctable benchmark configuration or safety failure."""


def _benchmark_ranking_policy() -> dict[str, Any]:
    return {
        "ranking_rule_version": RANKING_RULE_VERSION,
        "ranking_weights": dict(_BENCHMARK_RANKING_WEIGHTS),
        "favorite_bonus_policy": {
            "favorite": False,
            "applied": False,
            "value": DEFAULT_FAVORITE_BONUS,
            "mode": "disabled_for_golden_manifest",
        },
    }


def _production_ranking_score(result: Mapping[str, Any]) -> float:
    """Use the production scoring function without copying its arithmetic."""

    return calculate_ranking_score(
        result,
        _BENCHMARK_RANKING_WEIGHTS,
        favorite=False,
        favorite_bonus=DEFAULT_FAVORITE_BONUS,
    )


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
        "caption_min_chars": 10,
        "caption_target_chars": 60,
        "caption_max_chars": 100,
        "side_caption_min_chars": 8,
        "side_caption_target_chars": 12,
        "side_caption_max_chars": 16,
        "copy_default_style": "literary",
        "copy_humor_level": 2,
        "copy_poetic_level": 3,
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
    seen_ids: set[str] = set()
    for index, record in enumerate(records):
        category = _exclusion_category(record)
        if category is not None:
            excluded[category] += 1
            continue
        photo_id = str(_record_value(record, "photo_id", "id", default=f"row-{index}"))
        if photo_id in seen_ids:
            raise BenchmarkError(f"benchmark record duplicate id：{photo_id}")
        seen_ids.add(photo_id)
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
            "inactive": False,
            "ineligible": False,
            "missing": False,
            "manually_excluded": False,
        }
        for index, path in enumerate(_synthetic_images(directory, count, seed))
    ]


def _manifest_unknown_fields(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise BenchmarkError(f"golden manifest {label} 含未知欄位：{', '.join(unknown)}")


def _manifest_string(value: Any, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(char) < 0x20 or ord(char) == 0x7f for char in value)
    ):
        raise BenchmarkError(f"golden manifest {field} 必須是 1 到 {maximum} 字元字串")
    return value


def _manifest_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkError(f"golden manifest {field} 必須是有限數字")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BenchmarkError(f"golden manifest {field} 必須是有限數字") from exc
    if not math.isfinite(numeric):
        raise BenchmarkError(f"golden manifest {field} 必須是有限數字")
    return numeric


def _validate_manifest_rotation(value: Any, field: str) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value not in _MANIFEST_ROTATIONS
    ):
        raise BenchmarkError(f"golden manifest {field} rotation 不合法")


def _validate_manifest_expected(expected: Any) -> dict[str, Any]:
    if not isinstance(expected, dict):
        raise BenchmarkError("live quality dataset item 缺少 expected schema")
    _manifest_unknown_fields(expected, _MANIFEST_EXPECTED_KEYS, "expected")
    for field, maximum in (("memory_score", 100), ("visual_score", 100), ("special_level", 4)):
        if type(expected.get(field)) not in (int, float) or not 0 <= expected[field] <= maximum:
            raise BenchmarkError(f"golden manifest expected.{field} 不合法")
    if not isinstance(expected.get("should_keep"), bool):
        raise BenchmarkError("golden manifest expected.should_keep 必須是 Boolean")
    has_nested_orientation = "visual_orientation" in expected
    has_flat_orientation = "rotation_cw" in expected or "ambiguous" in expected
    if has_nested_orientation and has_flat_orientation:
        raise BenchmarkError("golden manifest expected orientation 不可同時使用 nested 與 flat alias")
    canonical = dict(expected)
    if has_nested_orientation:
        orientation = expected["visual_orientation"]
        if not isinstance(orientation, dict):
            raise BenchmarkError("golden manifest expected.visual_orientation 必須是 object")
        _manifest_unknown_fields(orientation, {"rotation_cw", "ambiguous"}, "visual_orientation")
        if set(orientation) != {"rotation_cw", "ambiguous"}:
            raise BenchmarkError("golden manifest expected.visual_orientation 欄位不完整")
        _validate_manifest_rotation(orientation["rotation_cw"], "expected.visual_orientation")
        if not isinstance(orientation["ambiguous"], bool):
            raise BenchmarkError("golden manifest expected.visual_orientation.ambiguous 必須是 Boolean")
        canonical["visual_orientation"] = dict(orientation)
    elif has_flat_orientation:
        if set(expected) & {"rotation_cw", "ambiguous"} != {"rotation_cw", "ambiguous"}:
            raise BenchmarkError("golden manifest flat orientation 欄位不完整")
        _validate_manifest_rotation(expected["rotation_cw"], "expected")
        if not isinstance(expected["ambiguous"], bool):
            raise BenchmarkError("golden manifest expected.ambiguous 必須是 Boolean")
        canonical["visual_orientation"] = {
            "rotation_cw": expected["rotation_cw"],
            "ambiguous": expected["ambiguous"],
        }
        canonical.pop("rotation_cw", None)
        canonical.pop("ambiguous", None)
    else:
        raise BenchmarkError("golden manifest expected 必須包含 orientation 欄位")
    return canonical


def _validate_manifest_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise BenchmarkError("golden manifest item 必須是 object")
    _manifest_unknown_fields(item, _MANIFEST_ITEM_KEYS, "item")
    _manifest_string(item.get("id"), "item.id", 160)
    _manifest_string(item.get("image"), "item.image", 300)
    canonical_expected = _validate_manifest_expected(item.get("expected"))
    for field in ("expected_rank", "expected_score"):
        if field in item:
            _manifest_number(item[field], f"item.{field}")
    for field in ("never_upload", "inactive", "ineligible", "missing", "manually_excluded"):
        if field in item and not isinstance(item[field], bool):
            raise BenchmarkError(f"golden manifest item.{field} 必須是 Boolean")
    canonical = dict(item)
    canonical["expected"] = canonical_expected
    return canonical


def _validate_manifest_path(path: Path, *, allowed_export_parent: bool) -> None:
    forbidden_parts = {"cache", "releases", "photos", "convertto6c_bmp-7"}
    normalized_parts = {part.casefold() for part in path.parts}
    if normalized_parts & forbidden_parts or (
        "data" in normalized_parts and not allowed_export_parent
    ):
        raise BenchmarkError("live quality dataset 路徑落在禁止的 production/cache/release/photo 目錄")


def _load_golden_records(dataset: Path) -> list[dict[str, Any]]:
    """Load a non-private golden manifest without accepting production paths."""

    resolved = dataset.expanduser().resolve()
    allowed_export_parent = any(
        parent.name.casefold() in {"benchmarks", "benchmarks_export", "golden"}
        for parent in resolved.parents
    )
    _validate_manifest_path(resolved, allowed_export_parent=allowed_export_parent)
    if not resolved.is_file():
        raise BenchmarkError("live quality dataset 必須是存在的 golden manifest JSON")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError("live quality dataset manifest 無法讀取") from exc
    if not isinstance(payload, dict):
        raise BenchmarkError("live quality dataset manifest 必須是 JSON object")
    _manifest_unknown_fields(payload, _MANIFEST_KEYS, "root")
    if payload.get("version") != "inktime-golden-v1":
        raise BenchmarkError("live quality dataset manifest version 必須是 inktime-golden-v1")
    dataset_meta = payload.get("dataset")
    if not isinstance(dataset_meta, dict):
        raise BenchmarkError("live quality dataset dataset 必須是 object")
    _manifest_unknown_fields(dataset_meta, _MANIFEST_DATASET_KEYS, "dataset")
    dataset_id = dataset_meta.get("id")
    dataset_source = dataset_meta.get("source")
    dataset_privacy = dataset_meta.get("privacy")
    if (
        not isinstance(dataset_id, str)
        or not dataset_id
        or len(dataset_id) > 120
        or not isinstance(dataset_source, str)
        or dataset_source not in {"committed_fixture", "synthetic"}
        or dataset_privacy != "non_private"
    ):
        raise BenchmarkError("live quality dataset 必須宣告 privacy=non_private")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise BenchmarkError("live quality dataset 必須包含至少一個 item")
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_images: set[Path] = set()
    for item in raw_items:
        canonical_item = _validate_manifest_item(item)
        photo_id = canonical_item["id"]
        if photo_id in seen_ids:
            raise BenchmarkError(f"golden manifest duplicate item id：{photo_id}")
        seen_ids.add(photo_id)
        try:
            image = (resolved.parent / canonical_item["image"]).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise BenchmarkError("live quality dataset image 路徑不合法") from exc
        _validate_manifest_path(image, allowed_export_parent=allowed_export_parent)
        if image == resolved or resolved.parent not in image.parents:
            raise BenchmarkError("live quality dataset image 必須位於 manifest 目錄內")
        if not canonical_item.get("missing", False) and not image.is_file():
            raise BenchmarkError("live quality dataset image 必須位於 manifest 目錄內")
        if image in seen_images:
            raise BenchmarkError(f"golden manifest duplicate resolved image：{image.name}")
        seen_images.add(image)
        records.append(
            {
                "photo_id": photo_id,
                "path": str(image),
                "expected": canonical_item["expected"],
                "expected_rank": canonical_item.get("expected_rank"),
                "expected_score": canonical_item.get("expected_score"),
                "never_upload": bool(canonical_item.get("never_upload", False)),
                "inactive": bool(canonical_item.get("inactive", False)),
                "ineligible": bool(canonical_item.get("ineligible", False)),
                "missing": bool(canonical_item.get("missing", False)),
                "manually_excluded": bool(canonical_item.get("manually_excluded", False)),
            }
        )
    return records


def _resize_fixture_images(images: Iterable[Path], directory: Path, max_side: int) -> list[Path]:
    """Materialize one bounded axis at its real maximum side; never upscale."""

    directory.mkdir(parents=True, exist_ok=True)
    resized: list[Path] = []
    for index, source in enumerate(images):
        target = directory / f"fixture-{index:04d}.jpg"
        with load_rgb(source, max_side) as image:
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


def _coverage_rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _new_metrics() -> dict[str, Any]:
    metrics = {
        "total_photos": 0,
        "selected_photos": 0,
        "attempted_photos": 0,
        "schema_valid_photos": 0,
        "quality_eligible_photos": 0,
        "ranking_eligible_photos": 0,
        "attempt_coverage_rate": None,
        "schema_valid_coverage_rate": None,
        "quality_coverage_rate": None,
        "ranking_coverage_rate": None,
        "quality_metrics_scope": "schema_valid_only",
        "ranking_metrics_scope": "schema_valid_and_rank_labeled_only",
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
        "known_cost_total": 0.0,
        "cost_complete": True,
        "cost_denominator": "attempted_photos",
        "avg_cost_per_attempted_photo": None,
        "cost_per_1000_attempted_photos": None,
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
    metrics.update(_benchmark_ranking_policy())
    return metrics


def _quality_prediction(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "memory_score": result["memory_score"], "visual_score": result["visual_score"],
        "special_level": result["special_level"], "types": list(result["types"]),
        "should_keep": evaluate_content_filter(dict(result["content_filter"]))["decision"] != "auto_excluded",
        "visual_orientation": dict(result["visual_orientation"]),
    }


def _contract_metrics_snapshot(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Keep quality/ranking results out of the contract metric stream."""

    return {
        key: value
        for key, value in metrics.items()
        if key not in {"quality_metrics", "ranking_metrics"}
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
            metrics["known_cost_total"] += cost
            return cost, True
        usage_missing = (
            usage.input_tokens <= 0
            and usage.output_tokens <= 0
            and usage.cached_tokens <= 0
            and usage.cache_write_tokens <= 0
            and usage.reasoning_tokens <= 0
        )
        estimated = None if usage_missing else provider.estimate_cost(model, usage)
        if estimated is not None:
            cost = max(0.0, float(estimated))
            metrics["estimated_cost"] = float(metrics["estimated_cost"] or 0) + cost
            metrics["known_cost_total"] += cost
            return cost, True
        metrics["unknown_cost_count"] += 1
        return None, False

    def run_offline(self, *, axes: list[BenchmarkAxis], sample_count: int, seed: str) -> dict[str, Any]:
        if sample_count < 1 or sample_count > MAX_SAMPLE_COUNT:
            raise BenchmarkError(f"sample-count 必須介於 1 到 {MAX_SAMPLE_COUNT}")
        report: dict[str, Any] = {
            "benchmark_version": "model-benchmark-v2",
            "mode": "offline-contract",
            "seed": seed,
            "sample_count": sample_count,
            "selected_count": 0,
            "dataset_source": "synthetic-generated-no-private-photos",
            "excluded_counts": {},
            "axes": [],
            "contract_metrics": [],
            "quality_metrics": None,
            "ranking_metrics": None,
            "ranking_policy": _benchmark_ranking_policy(),
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
                metrics["selected_photos"] = len(axis_images)
                metrics["avg_request_body_bytes"] = _average(item.get("request_body_bytes", 0) for item in request_metrics)
                metrics["avg_image_bytes"] = _average(item.get("image_bytes", 0) for item in request_metrics)
                metrics["avg_system_prompt_chars"] = _average(item.get("prompt_chars", 0) for item in request_metrics)
                metrics["avg_schema_chars"] = _average(item.get("schema_chars", 0) for item in request_metrics)
                report["axes"].append({"axis": axis.label, "metrics": metrics})
                report["contract_metrics"].append({"axis": axis.label, "metrics": metrics})
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
        dataset: Path | None = None,
        confirm_live_quality: bool = False,
    ) -> dict[str, Any]:
        if dataset is None:
            raise BenchmarkError("live quality benchmark 必須明確提供 --dataset")
        if not confirm_live_quality:
            raise BenchmarkError("live quality benchmark 必須明確提供 --confirm-live-quality")
        if not api_key.strip():
            raise BenchmarkError("live benchmark 必須明確提供 API Key")
        if max_requests < 1 or max_requests > MAX_REQUESTS:
            raise BenchmarkError(f"max-requests 必須介於 1 到 {MAX_REQUESTS}")
        if max_cost <= 0:
            raise BenchmarkError("max-cost 必須是正數")
        if sample_count < 1 or sample_count > MAX_SAMPLE_COUNT:
            raise BenchmarkError(f"sample-count 必須介於 1 到 {MAX_SAMPLE_COUNT}")
        report: dict[str, Any] = {
            "benchmark_version": "model-benchmark-v2",
            "mode": "live-quality",
            "seed": seed,
            "sample_count": sample_count,
            "selected_count": 0,
            "dataset_source": f"golden-manifest:{Path(dataset).name}",
            "excluded_counts": {},
            "axes": [],
            "contract_metrics": [],
            "quality_metrics": [],
            "ranking_metrics": [],
            "ranking_policy": _benchmark_ranking_policy(),
            "max_cost_policy": "bounded_post_response_stop",
            "stopped_by_budget": False,
            "network_invocations": 0,
            "production_mutations": 0,
        }
        with tempfile.TemporaryDirectory(prefix="inktime-benchmark-live-") as directory:
            records = _load_golden_records(dataset)
            selected, excluded = _select_benchmark_records(records, sample_count=sample_count, seed=seed)
            images = [Path(str(record["path"])) for record in selected]
            report["selected_count"] = len(images)
            report["excluded_counts"] = excluded
            if not selected:
                # A fully excluded manifest must be a zero-network result;
                # even the Provider capability probe is unnecessary.
                return report
            requests_used = 0
            spent = 0.0
            class _BudgetStop(Exception):
                pass

            for axis in axes:
                metrics = _new_metrics()
                axis_images = _resize_fixture_images(
                    images, Path(directory) / f"live-{len(report['axes']):03d}", axis.image_max_side
                )
                metrics["total_photos"] = len(axis_images)
                metrics["selected_photos"] = len(axis_images)
                latencies: list[float] = []
                request_sizes: list[float] = []
                image_sizes: list[float] = []
                prompt_sizes: list[float] = []
                schema_sizes: list[float] = []
                first_pass_success = 0
                repairs = 0
                quality_items: list[dict[str, Any]] = []
                provider = self._provider(axis, api_key=api_key, base_url=base_url)
                try:
                    if requests_used >= max_requests or report["stopped_by_budget"]:
                        report["stopped_by_budget"] = True
                        raise _BudgetStop
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
                    for record, image in zip(selected, axis_images, strict=True):
                        if requests_used >= max_requests or spent >= max_cost or report["stopped_by_budget"]:
                            report["stopped_by_budget"] = True
                            break
                        started = time.perf_counter()
                        response: ProviderResponse | None = None
                        validated_result: dict[str, Any] | None = None
                        request_metrics_for_photo: list[dict[str, int]] = []
                        try:
                            metrics["attempted_photos"] += 1
                            metrics["vision_requests"] += 1
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
                            request_metrics_for_photo.append(dict(response.request_metrics or provider.last_request_metrics or {}))
                            vision_cost, vision_cost_known = self._accumulate_usage(metrics, provider, axis.model, response)
                            if vision_cost is not None:
                                spent += vision_cost
                            if not vision_cost_known:
                                report["stopped_by_budget"] = True
                            try:
                                validated_result = validate_analysis_result(response.content)
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
                                        validated_result = validate_analysis_result(repair_response.content)
                                        metrics["success_count"] += 1
                                    except AnalysisValidationError:
                                        pass
                        except (ProviderHTTPError, OSError, ValueError):
                            # An attempted call without a response has no
                            # trustworthy usage/cost denominator entry.
                            metrics["unknown_cost_count"] += 1
                            report["stopped_by_budget"] = True
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
                            if validated_result is not None:
                                metrics["schema_valid_photos"] += 1
                                expected = dict(record.get("expected") or {})
                                quality_item: dict[str, Any] = {
                                    "id": str(record.get("photo_id") or image.name),
                                    "expected": expected,
                                    "predicted": _quality_prediction(validated_result),
                                }
                                if record.get("expected_score") is not None:
                                    quality_item["expected_score"] = record["expected_score"]
                                if record.get("expected_rank") is not None:
                                    quality_item["expected_rank"] = record["expected_rank"]
                                if (
                                    record.get("expected_score") is not None
                                    or record.get("expected_rank") is not None
                                ):
                                    features = PhotoPreprocessor().analyze(image).as_dict()
                                    validated_result["local_quality_score"] = local_candidate_score(features, evaluation=evaluate_local_quality(features, settings={"analysis.e6_prefilter_enabled": False}))
                                    quality_item["predicted_score"] = _production_ranking_score(validated_result)
                                quality_items.append(quality_item)
                except _BudgetStop:
                    pass
                finally:
                    provider.close()
                metrics["quality_eligible_photos"] = len(quality_items)
                metrics["actual_cost"] = metrics["provider_reported_cost"]
                metrics["cost_complete"] = metrics["unknown_cost_count"] == 0
                metrics["avg_cost_per_attempted_photo"] = (
                    round(float(metrics["known_cost_total"]) / metrics["attempted_photos"], 6)
                    if metrics["attempted_photos"] > 0 and metrics["cost_complete"]
                    else None
                )
                metrics["cost_per_1000_attempted_photos"] = (
                    round(float(metrics["avg_cost_per_attempted_photo"]) * 1000, 3)
                    if metrics["avg_cost_per_attempted_photo"] is not None
                    else None
                )
                metrics["avg_cost_per_photo"] = metrics["avg_cost_per_attempted_photo"]
                metrics["cost_per_1000_photos"] = metrics["cost_per_1000_attempted_photos"]
                metrics["first_pass_schema_success_rate"] = _coverage_rate(
                    first_pass_success, metrics["attempted_photos"]
                )
                metrics["schema_success_rate"] = _coverage_rate(
                    metrics["schema_valid_photos"], metrics["attempted_photos"]
                )
                metrics["repair_rate"] = _coverage_rate(repairs, metrics["attempted_photos"])
                metrics["failure_rate"] = _coverage_rate(
                    max(0, metrics["attempted_photos"] - metrics["schema_valid_photos"]),
                    metrics["attempted_photos"],
                )
                metrics["avg_latency_ms"] = _average(latencies)
                metrics["p50_latency_ms"] = _percentile(latencies, 0.50)
                metrics["p95_latency_ms"] = _percentile(latencies, 0.95)
                metrics["avg_request_body_bytes"] = _average(request_sizes)
                metrics["avg_image_bytes"] = _average(image_sizes)
                metrics["avg_system_prompt_chars"] = _average(prompt_sizes)
                metrics["avg_schema_chars"] = _average(schema_sizes)
                quality = calculate_benchmark_metrics(quality_items)
                metrics["quality_metrics"] = quality["quality_metrics"]
                metrics["ranking_metrics"] = quality["ranking_metrics"]
                metrics["ranking_eligible_photos"] = int(quality["ranking_metrics"]["count"])
                if metrics["quality_metrics"]["count"] != metrics["quality_eligible_photos"]:
                    raise BenchmarkError("quality metrics count 不得超過 quality-eligible photos")
                if metrics["ranking_metrics"]["count"] != metrics["ranking_eligible_photos"]:
                    raise BenchmarkError("ranking metrics count 不得超過 ranking-eligible photos")
                metrics["attempt_coverage_rate"] = (
                    _coverage_rate(metrics["attempted_photos"], metrics["selected_photos"])
                    if metrics["attempted_photos"] > 0
                    else None
                )
                metrics["schema_valid_coverage_rate"] = _coverage_rate(
                    metrics["schema_valid_photos"], metrics["attempted_photos"]
                )
                metrics["quality_coverage_rate"] = _coverage_rate(
                    metrics["quality_eligible_photos"], metrics["attempted_photos"]
                )
                metrics["ranking_coverage_rate"] = _coverage_rate(
                    metrics["ranking_eligible_photos"], metrics["attempted_photos"]
                )
                axis_report = {"axis": axis.label, "metrics": metrics}
                report["axes"].append(axis_report)
                report["contract_metrics"].append(
                    {"axis": axis.label, "metrics": _contract_metrics_snapshot(metrics)}
                )
                report["quality_metrics"].append(
                    {"axis": axis.label, "metrics": quality["quality_metrics"]}
                )
                report["ranking_metrics"].append(
                    {"axis": axis.label, "metrics": quality["ranking_metrics"]}
                )
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
        f"- Ranking rule: `{report['ranking_policy']['ranking_rule_version']}`",
        f"- Ranking weights: `{json.dumps(report['ranking_policy']['ranking_weights'], sort_keys=True)}`",
        f"- Favorite bonus policy: `{report['ranking_policy']['favorite_bonus_policy']['mode']}`",
        "",
        (
            "Offline reports are request-contract measurements only; they are not model quality or accuracy claims."
            if report["mode"] == "offline-contract"
            else "Live quality reports use only the explicitly supplied non-private golden manifest and bounded Provider calls."
        ),
        "",
        "| Axis | Selected | Attempted | Schema-valid | Quality coverage | Ranking coverage | Provider requests | Success | Known cost | Cost complete | Avg cost / attempted photo | Unknown cost |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|",
    ]
    if report["mode"] == "live-quality":
        lines.insert(
            8,
            "- max_cost: bounded post-response stop; unknown cost stops subsequent Provider requests and is never treated as zero.",
        )
    if report["mode"] == "live-quality":
        lines.extend(
            [
                "- cost_denominator: `attempted_photos`.",
                "- quality_metrics_scope: `schema_valid_only`; ranking_metrics_scope: `schema_valid_and_rank_labeled_only`.",
            ]
        )
    for item in report["axes"]:
        metrics = item["metrics"]
        lines.append(
            "| {axis} | {selected_photos} | {attempted_photos} | {schema_valid_photos} | "
            "{quality_coverage_rate} | {ranking_coverage_rate} | {provider_requests} | {success_count} | "
            "{known_cost_total} | {cost_complete} | {avg_cost_per_attempted_photo} | {unknown_cost_count} |".format(
                axis=item["axis"], **metrics
            )
        )
    lines.extend(
        [
            "",
            "The benchmark does not write `photo_analysis`, `releases`, `display_history`, or production AI cache.",
            "Quality and ranking metrics are conditional metrics. They include only schema-valid predictions and must be interpreted together with the reported coverage rates.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(report: dict[str, Any], *, output: Path, markdown_output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_output.write_text(markdown_report(report), encoding="utf-8")
