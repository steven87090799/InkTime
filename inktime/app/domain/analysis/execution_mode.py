"""Safe, backwards-compatible analysis execution modes.

The mode is deliberately independent from provider configuration.  A configured
API key must never make an installation start paid analysis by itself.
"""

from __future__ import annotations

EXECUTION_MODES = frozenset({"disabled", "local_only", "local_with_manual_ai", "automatic_ai"})


def legacy_execution_mode(ai_mode: object, *, local_processing_enabled: bool) -> str:
    """Map the retired scheduling policy to the conservative new authority."""
    value = str(ai_mode or "off")
    if value == "on_demand":
        return "local_with_manual_ai"
    if value in {"top_candidates", "eligible", "full_library"}:
        return "automatic_ai"
    return "local_only" if local_processing_enabled else "disabled"


def execution_mode(settings) -> str:
    """Read the new authority, with a safe adapter for pre-upgrade databases."""
    value = settings.get("analysis.execution_mode", None)
    if value in EXECUTION_MODES:
        return str(value)
    return legacy_execution_mode(
        settings.get("analysis.ai_mode", "off"),
        local_processing_enabled=bool(settings.get("analysis.prefilter_enabled", True)),
    )


def permits_automatic_ai(mode: str) -> bool:
    return mode == "automatic_ai"


def permits_manual_ai(mode: str) -> bool:
    return mode in {"local_with_manual_ai", "automatic_ai"}
