"""Read-only configuration snapshots; never scan photos or enqueue work."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

from inktime.app.domain.analysis.execution_mode import execution_mode
from inktime.app.web.ai_readiness import MODE_LABELS, ai_readiness_snapshot


COMMON_GROUPS = (
    ("照片分析範圍", ("analysis.execution_mode", "analysis.ai_mode", "analysis.seasonal_window_days", "analysis.ai_top_n", "analysis.strategy")),
    ("模型與處理方式", ("model.analysis_model", "model.low_model", "model.repair_model", "batch.model", "analysis.image_max_side", "analysis.concurrency")),
    ("用量與費用限制", ("analysis.ai_daily_photo_limit", "analysis.ai_monthly_photo_limit", "budget.daily_stop", "budget.monthly_stop", "budget.photo_max", "budget.max_tokens")),
    ("照片篩選與選片", ("analysis.prefilter_enabled", "analysis.prefilter_screenshots", "analysis.prefilter_low_quality", "analysis.e6_min_score", "render.selection_mode", "render.seasonal_priority_min_score")),
    ("顏色與相框版面", ("render.profile", "render.dither", "render.dither_strength", "render.color_distance", "render.layout", "render.frame_orientation", "render.fit_mode")),
    ("時區與備份", ("general.timezone", "backup.schedule_enabled", "backup.hour", "backup.retention")),
)
SCOPE_LABELS = {
    "dynamic": "動態套用", "next_job": "下次工作", "next_render": "下次渲染",
    "future_device_only": "之後新增的裝置", "restart": "重新啟動後", "not_wired": "尚未接上 Runtime",
}


def _value_label(value: Any, definition: dict) -> str:
    if definition.get("visibility") == "sensitive":
        return str(value.get("status", "內容隱藏")) if isinstance(value, dict) else "內容隱藏"
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "啟用" if value else "停用"
    labels = definition.get("choice_labels") or {}
    if isinstance(value, str) and value in labels:
        return str(labels[value])
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value) if value != "" else "未設定"


def settings_report_snapshot(settings, providers, provider_service, schedules, devices) -> dict:
    rows = settings.all(redact_sensitive=True)
    values = {row["key"]: row["value"] for row in rows}
    snapshot_settings = SimpleNamespace(get=values.get)
    mode = execution_mode(snapshot_settings)
    provider_rows = providers.list()
    readiness = ai_readiness_snapshot(
        snapshot_settings, SimpleNamespace(list=lambda: provider_rows), provider_service
    )
    for detail, provider in zip(readiness["provider_details"], provider_rows, strict=True):
        detail.update({key: provider.get(key) for key in (
            "priority", "enabled", "supports_batch", "rate_limit_rpm", "token_limit_tpm",
            "max_concurrency", "timeout_seconds",
        )})
    categories: dict[str, list[dict]] = {}
    by_key = {}
    for row in rows:
        definition = row["definition"]
        dependencies = definition.get("dependencies") or []
        applies = all(
            values.get(dep["key"]) == dep["equals"] if "equals" in dep
            else values.get(dep["key"]) in dep.get("one_of", [])
            for dep in dependencies
        )
        entry = {
            "key": row["key"], "label": definition["label_zh_tw"],
            "description": definition["description"],
            "value": _value_label(row["value"], definition),
            "default": _value_label(definition.get("default"), definition),
            "source": "系統設定" if row["updated_by"] else "預設值",
            "scope": SCOPE_LABELS.get(definition["effective_scope"], definition["effective_scope"]),
            "wired": bool(definition["runtime_wired"]), "applies": applies,
            "note": definition.get("effective_note"), "updated_at": row["updated_at"],
            "minimum": definition.get("min"), "maximum": definition.get("max"),
            "url": "/settings?" + urlencode({"search": row["key"]}) + "#setting-" + row["key"].replace(".", "-"),
        }
        by_key[row["key"]] = entry
        categories.setdefault(row["category"], []).append(entry)
    descriptions = {
        "disabled": "不建立照片分析流程；既有結果仍保留。",
        "local_only": "只執行本機特徵與品質分析，不呼叫模型。",
        "local_with_manual_ai": "平時只做本機分析；只有手動送入 AI 時才呼叫模型。",
        "automatic_ai": "可自動呼叫已設定的模型；AI 範圍依候選模式、排程與單次工作設定決定。",
        "seasonal_ai": (
            f"全年每日運作，搜尋歷年同日前後 {values.get('analysis.seasonal_window_days', 5)} 天；"
            "只補尚未分析的新內容，已送模型的內容不因日期移動或模型升級重送。"
        ),
    }
    task_rows = []
    for task in schedules.list():
        config = task["config"]
        task_rows.append({
            key: task.get(key) for key in (
                "key", "name", "kind", "enabled", "cron", "timezone", "next_run", "last_success",
                "last_failure", "start_time", "window_start", "window_end", "timeout_seconds", "retry_count",
            )
        } | {"mode": config.get("mode"), "strategy": config.get("strategy"),
             "concurrency": config.get("concurrency"), "batch_size": config.get("batch_size")})
    device_rows = []
    for row in devices.list():
        device = dict(row)
        device_rows.append({key: device.get(key) for key in (
            "name", "enabled", "delivery_mode", "panel_profile", "frame_orientation", "layout_mode", "fit_mode",
            "timezone", "schedule", "config_version", "acked_config_version", "last_seen_at",
        )})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(), "setting_count": len(rows),
        "categories": categories, "common_groups": [
            {"title": title, "rows": [by_key[key] for key in keys if key in by_key]}
            for title, keys in COMMON_GROUPS
        ],
        "execution_mode": mode, "mode_label": MODE_LABELS[mode],
        "mode_description": descriptions[mode], "readiness": readiness,
        "uses_model": mode in {"local_with_manual_ai", "automatic_ai", "seasonal_ai"},
        "schedules": task_rows, "devices": device_rows,
    }
