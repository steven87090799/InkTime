from __future__ import annotations

from typing import Any

from inktime.app.domain.analysis.execution_mode import execution_mode


MODE_LABELS = {
    "disabled": "完全停用",
    "local_only": "僅使用本機選片",
    "local_with_manual_ai": "本機選片＋手動 AI",
    "automatic_ai": "自動 AI 分析",
}


def ai_readiness_snapshot(settings, provider_repository, provider_service) -> dict[str, Any]:
    """Describe every non-network condition required to create a Vision job."""

    mode = execution_mode(settings)
    fallback_model = str(settings.get("model.analysis_model", "gpt-4o") or "").strip()
    routes = provider_service.usable_route_snapshot()
    usable_ids = {str(item["provider_id"]) for item in routes}
    route_models = [
        {
            "provider": str(item.get("display_name") or item.get("provider_id") or "Provider"),
            "model": str(item.get("model") or fallback_model).strip(),
        }
        for item in routes
    ]
    resolved_model_label = "、".join(
        f"{item['provider']}：{item['model']}" for item in route_models if item["model"]
    )
    provider_details = []
    for row in provider_repository.list():
        issues = []
        if not bool(row.get("enabled")):
            issues.append("尚未啟用")
        if not bool(row.get("supports_vision")):
            issues.append("不具 Vision 能力")
        if not str(row.get("base_url") or "").strip():
            issues.append("Base URL 空白")
        if (
            str(row.get("kind") or "").casefold() != "ollama"
            and not bool(row.get("api_key_configured"))
        ):
            issues.append("尚未設定 API Key")
        provider_details.append(
            {
                "name": str(row.get("name") or row.get("id") or "未命名 Provider"),
                "kind": str(row.get("kind") or "unknown"),
                "model": str(row.get("model") or fallback_model or "未設定"),
                "ready": str(row.get("id")) in usable_ids,
                "issues": tuple(issues),
            }
        )

    checks = (
        {
            "key": "execution_mode",
            "label": "分析執行模式",
            "ready": mode == "automatic_ai",
            "current": MODE_LABELS.get(mode, mode),
            "required": "必須設為「自動 AI 分析」",
            "action_label": "前往設定並直接搜尋",
            "action_url": "/settings?search=分析執行模式",
        },
        {
            "key": "provider",
            "label": "Vision Provider",
            "ready": bool(usable_ids),
            "current": f"{len(usable_ids)} 個通過靜態設定檢查",
            "required": "至少一個 Provider 必須啟用、支援 Vision、具有 Base URL，並設定 API Key（本機 Ollama 除外）",
            "action_label": "前往模型與 API",
            "action_url": "/providers",
        },
        {
            "key": "model",
            "label": "實際路由模型",
            "ready": bool(resolved_model_label or fallback_model),
            "current": resolved_model_label or fallback_model or "未設定",
            "required": (
                "每個可用 Provider 都必須指定模型；"
                "openrouter/free 代表由免費路由器於請求時選擇"
            ),
            "action_label": "前往選擇模型",
            "action_url": "/providers",
        },
    )
    ready_count = sum(1 for check in checks if check["ready"])
    return {
        "ready": ready_count == len(checks),
        "ready_count": ready_count,
        "required_count": len(checks),
        "checks": checks,
        "provider_details": tuple(provider_details),
        "model": resolved_model_label or fallback_model,
        "execution_mode": mode,
    }
