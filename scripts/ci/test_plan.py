"""Build the source-owned test plan for InkTime hosted CI.

This module is intentionally dependency-free and pure at its planning boundary.
Path ownership lives here so that GitHub Actions only has to execute the
selected suites and gates.  Unknown repository-relative paths fail open to the
full plan; known CI/documentation/test-only changes remain bounded in Draft
mode.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping
from typing import Any

IMPACT_MODE = "impact"
FULL_MODE = "full"

DOMAIN_ORDER = (
    "python",
    "web_ui",
    "auth_security",
    "runtime",
    "queue_resilience",
    "persistence",
    "migration",
    "backup_restore",
    "device",
    "render_release",
    "scanner_photos",
    "notifications_observability",
    "settings_governance",
    "provider_ai",
    "docker",
    "tls_security",
    "firmware",
    "benchmark",
    "dependencies",
    "dev_dependencies",
    "e2e_tooling",
    "ci_config",
    "test_surface",
    "docs",
    "unknown",
)

PRODUCTION_DOMAINS = frozenset(
    {
        "python",
        "web_ui",
        "auth_security",
        "runtime",
        "queue_resilience",
        "persistence",
        "migration",
        "backup_restore",
        "device",
        "render_release",
        "scanner_photos",
        "notifications_observability",
        "settings_governance",
        "provider_ai",
        "docker",
        "tls_security",
        "firmware",
        "benchmark",
        "dependencies",
    }
)

TIER_0_SUITES = (
    "changed_path_classification",
    "ci_planner_contracts",
    "ruff",
    "mypy",
)

TIER_0_GATES = (
    "secret_scan",
)

FULL_TEST_SUITES = (
    "python312_unit_security_integration_coverage",
    "python310_compatibility_tests",
    "ci_routing_contracts",
    "python_application_owner",
    "python_dependency_owner",
    "web_ui_owner",
    "web_api_owner",
    "web_e2e",
    "auth_security_owner",
    "runtime_scheduler_owner",
    "queue_resilience_owner",
    "persistence_owner",
    "migration_owner",
    "backup_restore_owner",
    "device_api_contract_owner",
    "device_delivery_owner",
    "render_release_owner",
    "scanner_photos_owner",
    "notifications_observability_owner",
    "settings_governance_owner",
    "provider_analysis_owner",
    "docker_runtime_owner",
    "container_configuration_owner",
    "tls_configuration_owner",
    "firmware_host_contract_tests",
    "benchmark_contract",
)

FULL_REQUIRED_STATIC_SUITES = ("dependency_policy",)
FULL_PLAN_SUITES = TIER_0_SUITES + FULL_REQUIRED_STATIC_SUITES + FULL_TEST_SUITES

SELECTED_SUITE_RUNNER = "selected_owner_suites"
SUITE_EXECUTION_OWNERS = {
    "changed_path_classification": "changes",
    "ci_planner_contracts": SELECTED_SUITE_RUNNER,
    "ruff": "python_quality",
    "mypy": "python_quality",
    "dependency_policy": "python_quality",
    "python312_unit_security_integration_coverage": "python_quality",
    "python310_compatibility_tests": "python_compatibility",
    "ci_routing_contracts": SELECTED_SUITE_RUNNER,
    "python_application_owner": SELECTED_SUITE_RUNNER,
    "python_dependency_owner": "dependency_audit",
    "web_ui_owner": SELECTED_SUITE_RUNNER,
    "web_api_owner": SELECTED_SUITE_RUNNER,
    "web_e2e": "playwright",
    "auth_security_owner": SELECTED_SUITE_RUNNER,
    "runtime_scheduler_owner": SELECTED_SUITE_RUNNER,
    "queue_resilience_owner": SELECTED_SUITE_RUNNER,
    "persistence_owner": SELECTED_SUITE_RUNNER,
    "migration_owner": "migration_contract",
    "backup_restore_owner": "docker_lan_persistence",
    "device_api_contract_owner": SELECTED_SUITE_RUNNER,
    "device_delivery_owner": SELECTED_SUITE_RUNNER,
    "render_release_owner": SELECTED_SUITE_RUNNER,
    "scanner_photos_owner": SELECTED_SUITE_RUNNER,
    "notifications_observability_owner": SELECTED_SUITE_RUNNER,
    "settings_governance_owner": SELECTED_SUITE_RUNNER,
    "provider_analysis_owner": SELECTED_SUITE_RUNNER,
    "docker_runtime_owner": "container_security",
    "container_configuration_owner": "container_security",
    "tls_configuration_owner": "tls_smoke",
    "firmware_host_contract_tests": "firmware_host_contract",
    "benchmark_contract": "benchmark",
    "docs_contract": "docs",
    "unit_owner": SELECTED_SUITE_RUNNER,
    "integration_owner": SELECTED_SUITE_RUNNER,
}

FULL_EXPENSIVE_GATES = (
    "python312_full",
    "python310_compatibility",
    "dependency_audit",
    "migration",
    "runtime_soak",
    "playwright",
    "docker_lan_persistence",
    "tls_smoke",
    "firmware_host_contract",
    "firmware_full_matrix",
    "container_security",
    "benchmark",
    "repository_gate",
    "container_security_gate",
)

ALL_FULL_GATES = frozenset(FULL_EXPENSIVE_GATES)
IMPACT_ONLY_GATES = frozenset({"firmware_quick", "firmware_affected"})

DOMAIN_OWNER_SUITES = {
    "python": ("python_application_owner",),
    "web_ui": ("web_ui_owner", "web_api_owner", "web_e2e"),
    "auth_security": ("auth_security_owner",),
    "runtime": ("runtime_scheduler_owner",),
    "queue_resilience": ("queue_resilience_owner",),
    "persistence": ("persistence_owner",),
    "migration": ("migration_owner",),
    "backup_restore": ("backup_restore_owner",),
    "device": ("device_api_contract_owner", "device_delivery_owner"),
    "render_release": ("render_release_owner",),
    "scanner_photos": ("scanner_photos_owner",),
    "notifications_observability": ("notifications_observability_owner",),
    "settings_governance": ("settings_governance_owner",),
    "provider_ai": ("provider_analysis_owner",),
    "docker": ("docker_runtime_owner", "container_configuration_owner"),
    "tls_security": ("tls_configuration_owner",),
    "firmware": ("firmware_host_contract_tests",),
    "benchmark": ("benchmark_contract",),
    "dependencies": ("python_dependency_owner", "dependency_policy"),
}

FULL_FIRMWARE_PROFILES = (
    "gdey_release",
    "gdep_release",
    "photopainter_release",
    "trusted_lan_gdey",
    "trusted_lan_gdep",
    "trusted_lan_photopainter",
    "default_debug",
    "photopainter_debug",
)
PRIMARY_FIRMWARE_PROFILE = "photopainter_release"

GATE_ORDER = TIER_0_GATES + ("actionlint",) + FULL_EXPENSIVE_GATES

IMPACT_GATE_EXECUTION = {
    "secret_scan": "impact:secret_scan",
    "actionlint": "impact:actionlint",
    "dependency_audit": "impact:dependency_audit",
    "runtime_soak": "impact:runtime_soak",
    "playwright": "impact:playwright",
    "docker_lan_persistence": "impact:docker_lan_persistence",
    "tls_smoke": "impact:tls_smoke",
    "firmware_host_contract": "impact:firmware_host_contract",
    "firmware_quick": "impact:firmware_quick",
    "firmware_affected": "impact:firmware_affected",
    "container_security": "impact:container_security",
    "benchmark": "impact:benchmark",
}

FULL_GATE_EXECUTION = {
    "secret_scan": "full:secret_scan",
    "actionlint": "full:actionlint",
    "python312_full": "full:python312_full",
    "python310_compatibility": "full:python310_compatibility",
    "dependency_audit": "full:dependency_audit",
    "migration": "full:migration",
    "runtime_soak": "full:runtime_soak",
    "playwright": "full:playwright",
    "docker_lan_persistence": "full:docker_lan_persistence",
    "tls_smoke": "full:tls_smoke",
    "firmware_host_contract": "full:firmware_host_contract",
    "firmware_full_matrix": "full:firmware_full_matrix",
    "container_security": "full:container_security",
    "benchmark": "full:benchmark",
    "repository_gate": "full:repository_gate",
    "container_security_gate": "full:container_security_gate",
}

IMPACT_TO_FULL_EXECUTION = {
    IMPACT_GATE_EXECUTION[impact_gate]: FULL_GATE_EXECUTION[full_gate]
    for impact_gate, full_gate in {
        "secret_scan": "secret_scan",
        "actionlint": "actionlint",
        "dependency_audit": "dependency_audit",
        "runtime_soak": "runtime_soak",
        "playwright": "playwright",
        "docker_lan_persistence": "docker_lan_persistence",
        "tls_smoke": "tls_smoke",
        "firmware_host_contract": "firmware_host_contract",
        "firmware_quick": "firmware_full_matrix",
        "firmware_affected": "firmware_full_matrix",
        "container_security": "container_security",
        "benchmark": "benchmark",
    }.items()
}

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})


def _normalise_path(path: object) -> str:
    value = str(path).strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value


def _ordered(values: Iterable[str], order: Iterable[str]) -> list[str]:
    value_set = set(values)
    known = [value for value in order if value in value_set]
    known_set = set(known)
    return known + sorted(value_set - known_set)


def _is_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().casefold() in _TRUE_VALUES


def _optional_bool(context: Mapping[str, object], *keys: str) -> bool | None:
    for key in keys:
        if key not in context:
            continue
        value = context[key]
        if isinstance(value, bool):
            return value
        normalised = str(value).strip().casefold()
        if normalised in _TRUE_VALUES:
            return True
        if normalised in _FALSE_VALUES:
            return False
    return None


def _labels(context: Mapping[str, object]) -> set[str]:
    raw_labels = context.get("labels", context.get("pull_request_labels", []))
    if isinstance(raw_labels, str):
        return {raw_labels}
    if not isinstance(raw_labels, Iterable):
        return set()

    result: set[str] = set()
    for raw_label in raw_labels:
        if isinstance(raw_label, Mapping):
            raw_label = raw_label.get("name", "")
        label = str(raw_label).strip()
        if label:
            result.add(label)
    return result


def _optional_text(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _firmware_profiles_for_path(path: str) -> tuple[set[str], str | None]:
    """Return the supported firmware profiles affected by a firmware surface."""

    lower_path = path.casefold()
    if path.startswith("tests/firmware/"):
        return set(FULL_FIRMWARE_PROFILES), "firmware_host_contract_tests"

    if path.startswith("inktime/app/api/") or any(
        token in lower_path for token in ("device_delivery", "device_manifest", "device_token", "/ack")
    ):
        return set(FULL_FIRMWARE_PROFILES), "server_device_contract"

    if path.endswith(".ino"):
        if "gdep" in lower_path:
            return {"gdep_release", "trusted_lan_gdep"}, "gdep_profile"
        if "gdey" in lower_path:
            return {"gdey_release", "trusted_lan_gdey", "default_debug"}, "gdey_profile"
        if "photopainter" in lower_path:
            return {
                "photopainter_release",
                "trusted_lan_photopainter",
                "photopainter_debug",
            }, "photopainter_profile"
        return set(FULL_FIRMWARE_PROFILES), "shared_profile_or_build_surface"

    if any(
        token in lower_path
        for token in (
            "hardware_profile",
            "device_config_store",
            "device_http_transport",
            "offline_schedule_core",
            "pairing_recovery_core",
            "queue_client_core",
            "queue_runtime_types",
            "power_policy",
            "power_manager",
            "nvs",
            "panel",
            "schema",
            "tls",
            "build",
            "compile",
        )
    ):
        return set(FULL_FIRMWARE_PROFILES), "shared_profile_or_build_surface"

    if "photopainter" in lower_path or "photo" in lower_path:
        return {
            "photopainter_release",
            "trusted_lan_photopainter",
            "photopainter_debug",
        }, "photopainter_profile"

    if "gdep" in lower_path:
        return {"gdep_release", "trusted_lan_gdep"}, "gdep_profile"

    if "gdey" in lower_path:
        return {"gdey_release", "trusted_lan_gdey", "default_debug"}, "gdey_profile"

    return set(FULL_FIRMWARE_PROFILES), "unclassified_firmware_surface"


def _test_path_plan(path: str) -> tuple[set[str], set[str], set[str]]:
    """Return domains, owner suites, and bounded gates for a test-only path."""

    domains = {"test_surface"}
    suites: set[str] = set()
    gates: set[str] = set()
    lower_path = path.casefold()
    filename = path.rsplit("/", 1)[-1].casefold()

    direct_firmware_test = path.startswith("tests/firmware/") or any(
        token in lower_path
        for token in (
            "/test_esp32_",
            "test_photopainter_",
            "test_device_",
            "test_firmware_",
            "test_tls_device",
        )
    )

    if path.startswith("tests/e2e/") or path.endswith(("_e2e.py", "web_smoke.py")):
        domains.add("web_ui")
        suites.add("web_e2e")
        gates.add("playwright")
    if direct_firmware_test:
        domains.add("firmware")
        suites.add("firmware_host_contract_tests")
        gates.add("firmware_host_contract")
    if (
        "ci_changed_paths" in filename
        or "ci_test_plan" in filename
        or "ci_selected_suites" in filename
    ):
        domains.add("ci_config")
        suites.add("ci_routing_contracts")
    if "migration" in lower_path:
        domains.add("persistence")
        domains.add("migration")
        suites.update({"persistence_owner", "migration_owner"})
    if "backup" in lower_path or "restore" in lower_path:
        domains.add("persistence")
        domains.add("backup_restore")
        suites.update({"persistence_owner", "backup_restore_owner"})
    if "sqlite" in lower_path or "repository" in lower_path:
        domains.add("persistence")
        suites.add("persistence_owner")
    if any(token in lower_path for token in ("scheduler", "worker", "timeout", "runtime", "offline", "jobs")):
        domains.add("runtime")
        suites.add("runtime_scheduler_owner")
        gates.add("runtime_soak")
    if any(token in lower_path for token in ("resilience", "queue", "ack", "device")):
        domains.update({"queue_resilience", "device"})
        suites.update({"queue_resilience_owner", "device_delivery_owner"})
        gates.add("runtime_soak")
    if any(token in lower_path for token in ("provider", "openrouter", "analysis", "scoring")):
        domains.add("provider_ai")
        suites.add("provider_analysis_owner")
    if any(token in lower_path for token in ("auth", "session", "csrf", "security", "token")):
        domains.add("auth_security")
        suites.add("auth_security_owner")
    if any(token in lower_path for token in ("tls", "proxy", "https")):
        domains.add("tls_security")
        suites.add("tls_configuration_owner")
        gates.add("tls_smoke")
    if any(token in lower_path for token in ("session", "login", "browser", "web_smoke")):
        domains.add("web_ui")
        suites.add("web_e2e")
        gates.add("playwright")
    if "management_ui" in lower_path:
        domains.add("web_ui")
        suites.add("web_api_owner")
    if path.startswith("tests/security/"):
        domains.add("auth_security")
        suites.add("auth_security_owner")
    if path.startswith("tests/integration/"):
        suites.add("integration_owner")

    if not suites:
        suites.add("unit_owner")

    return domains, suites, gates


def _classify_path(path: str) -> tuple[set[str], set[str], set[str], bool]:
    """Classify one normalized path into domains, suites, gates, and unknown."""

    domains: set[str] = set()
    suites: set[str] = set()
    gates: set[str] = set()
    lower_path = path.casefold()

    # This path is both CI-owned and a real production TLS/container surface.
    if path.startswith(".github/tls-smoke/"):
        domains.update({"ci_config", "docker", "tls_security"})
        suites.update(
            {
                "ci_routing_contracts",
                "tls_configuration_owner",
                "docker_runtime_owner",
            }
        )
        gates.update({"actionlint", "tls_smoke", "container_security"})
        return domains, suites, gates, False

    if path == "AGENTS.md" or path.startswith(".github/"):
        domains.add("ci_config")
        suites.add("ci_routing_contracts")
        gates.add("actionlint")
        return domains, suites, gates, False

    if path.startswith("tests/"):
        return (*_test_path_plan(path), False)

    is_markdown = path.startswith(("README", "LICENSE")) or path.startswith("docs/") or path.endswith(".md")
    if is_markdown:
        domains.add("docs")
        suites.add("docs_contract")
        return domains, suites, gates, False

    if path == "requirements.txt" or path.startswith(("constraints/", "constraints-")):
        domains.add("dependencies")
        suites.update({"python_dependency_owner", "dependency_policy"})
        gates.update({"dependency_audit", "container_security"})
        return domains, suites, gates, False

    if path == "pyproject.toml":
        domains.add("dev_dependencies")
        suites.update({"dependency_policy", "ruff", "mypy"})
        return domains, suites, gates, False

    if path in {"Pipfile.lock", "poetry.lock", "uv.lock"}:
        domains.add("dependencies")
        suites.update({"python_dependency_owner", "dependency_policy"})
        gates.update({"dependency_audit", "container_security"})
        return domains, suites, gates, False

    if path in {"requirements-dev.txt", "tox.ini", "setup.cfg"}:
        domains.add("dev_dependencies")
        suites.add("dependency_policy")
        return domains, suites, gates, False

    if path == "requirements-e2e.txt":
        domains.add("e2e_tooling")
        suites.add("web_e2e")
        gates.add("playwright")
        return domains, suites, gates, False

    if path.startswith("Dockerfile") or path in {".dockerignore", ".trivyignore"}:
        domains.add("docker")
        suites.add("container_configuration_owner")
        if path.startswith("Dockerfile"):
            suites.add("docker_runtime_owner")
        gates.add("container_security")
        return domains, suites, gates, False

    if path.startswith("docker-compose"):
        domains.add("docker")
        suites.update({"docker_runtime_owner", "container_configuration_owner"})
        gates.update({"docker_lan_persistence", "container_security"})
        return domains, suites, gates, False

    if path.startswith("esp32/"):
        domains.add("firmware")
        suites.add("firmware_host_contract_tests")
        gates.update({"firmware_host_contract", "firmware_quick"})
        profiles, _reason = _firmware_profiles_for_path(path)
        if profiles - {PRIMARY_FIRMWARE_PROFILE}:
            gates.add("firmware_affected")
        return domains, suites, gates, False

    if path in {"server.py", "analyze_photos.py"}:
        domains.add("python")
        suites.add("python_application_owner")
        return domains, suites, gates, False

    if path.startswith("scripts/"):
        if path.startswith("scripts/ci/"):
            domains.add("ci_config")
            suites.add("ci_routing_contracts")
            gates.add("actionlint")
            return domains, suites, gates, False
        if path in {"scripts/production_preflight.py", "scripts/production_tls_smoke.py"}:
            domains.add("tls_security")
            suites.add("tls_configuration_owner")
            gates.add("tls_smoke")
            return domains, suites, gates, False
        if path in {"scripts/lan_production_gate.py", "scripts/production_compose_smoke.py"}:
            domains.update({"docker", "persistence", "runtime"})
            suites.update(
                {
                    "docker_runtime_owner",
                    "persistence_owner",
                    "runtime_scheduler_owner",
                }
            )
            gates.update({"docker_lan_persistence", "runtime_soak", "container_security"})
            return domains, suites, gates, False
        if path == "scripts/build_release_image.sh":
            domains.add("docker")
            suites.update({"docker_runtime_owner", "container_configuration_owner"})
            gates.add("container_security")
            return domains, suites, gates, False
        if path in {"scripts/restore_backup.py", "scripts/migrate.py"}:
            domains.add("persistence")
            if "restore" in path:
                domains.add("backup_restore")
                suites.update({"persistence_owner", "backup_restore_owner"})
            else:
                domains.add("migration")
                suites.update({"persistence_owner", "migration_owner"})
            gates.add("docker_lan_persistence")
            return domains, suites, gates, False
        if path == "scripts/runtime_soak.py":
            domains.add("runtime")
            suites.add("runtime_scheduler_owner")
            gates.add("runtime_soak")
            return domains, suites, gates, False
        if path in {"scripts/benchmark_models.py", "scripts/performance_100k.py"}:
            domains.add("benchmark")
            suites.add("benchmark_contract")
            gates.add("benchmark")
            return domains, suites, gates, False
        if path == "scripts/check_dependency_policy.py":
            domains.add("dev_dependencies")
            suites.add("dependency_policy")
            return domains, suites, gates, False
        if path.endswith(".py"):
            domains.add("python")
            suites.add("python_application_owner")
            return domains, suites, gates, False

    if path.startswith(("inktime/app/web/templates/", "inktime/app/web/static/")):
        domains.add("web_ui")
        suites.add("web_ui_owner")
        gates.add("playwright")
        return domains, suites, gates, False

    if path.startswith("inktime/app/"):
        if path.endswith(".py"):
            domains.add("python")
            suites.add("python_application_owner")
        else:
            return {"unknown"}, set(), set(), True

        if path.startswith("inktime/app/web/"):
            domains.add("web_ui")
            suites.add("web_api_owner")
            gates.add("playwright")

        if path.startswith(
            (
                "inktime/app/core/security",
                "inktime/app/core/webhook_safety",
                "inktime/app/api/auth",
                "inktime/app/api/device_auth",
                "inktime/app/api/device_pairing",
                "inktime/app/domain/auth",
                "inktime/app/repositories/auth",
                "inktime/app/web/access",
            )
        ) or any(token in lower_path for token in ("/auth", "/session", "/csrf", "cookie", "proxy")):
            domains.add("auth_security")
            suites.add("auth_security_owner")
            if any(token in lower_path for token in ("/session", "/csrf", "cookie", "proxy")):
                domains.add("tls_security")
                suites.add("tls_configuration_owner")
                gates.add("tls_smoke")
                domains.add("web_ui")
                suites.add("web_e2e")
                gates.add("playwright")
            if path.startswith(("inktime/app/api/auth", "inktime/app/api/device_auth")) or (
                path.startswith("inktime/app/api/") and any(
                    token in lower_path for token in ("/session", "/csrf")
                )
            ):
                domains.add("web_ui")
                suites.add("web_api_owner")

        if path.startswith(("inktime/app/workers/", "inktime/app/services/scheduler")) or any(
            token in lower_path
            for token in (
                "/services/jobs",
                "/services/batch",
                "/workers/",
                "offline_schedule",
                "release_coordinator",
            )
        ):
            domains.add("runtime")
            suites.add("runtime_scheduler_owner")
            gates.add("runtime_soak")

        if any(
            token in lower_path
            for token in (
                "/queue",
                "resilience",
                "device_queue",
                "device_manifest",
                "offline_schedule",
                "scheduled_release",
            )
        ):
            domains.update({"runtime", "queue_resilience"})
            suites.update({"runtime_scheduler_owner", "queue_resilience_owner"})
            gates.add("runtime_soak")

        if path.startswith("inktime/app/db/") or path.startswith("inktime/app/repositories/") or any(
            token in lower_path for token in ("sqlite", "migration", "backup", "restore", "connection")
        ):
            domains.add("persistence")
            suites.add("persistence_owner")
            if "migration" in lower_path:
                domains.add("migration")
                suites.add("migration_owner")
            if any(token in lower_path for token in ("backup", "restore")):
                domains.add("backup_restore")
                suites.add("backup_restore_owner")
                gates.add("docker_lan_persistence")

        if path.startswith(
            (
                "inktime/app/api/devices",
                "inktime/app/api/device_",
                "inktime/app/services/device",
                "inktime/app/repositories/devices",
            )
        ) or any(token in lower_path for token in ("manifest", "ack", "device_delivery", "device_token")):
            domains.add("device")
            suites.update({"device_api_contract_owner", "device_delivery_owner"})
            if any(token in lower_path for token in ("manifest", "ack", "delivery")):
                domains.add("queue_resilience")
                suites.add("queue_resilience_owner")
            if (
                path.startswith(
                    (
                        "inktime/app/api/devices",
                        "inktime/app/api/device_",
                        "inktime/app/services/device",
                        "inktime/app/repositories/devices",
                    )
                )
                or any(
                    token in lower_path
                    for token in ("manifest", "ack", "device_delivery", "device_token", "pairing")
                )
            ):
                domains.add("firmware")
                suites.add("firmware_host_contract_tests")
                gates.update({"firmware_host_contract", "firmware_quick"})
                profiles, _reason = _firmware_profiles_for_path(path)
                if profiles - {PRIMARY_FIRMWARE_PROFILE}:
                    gates.add("firmware_affected")

        if path.startswith(
            (
                "inktime/app/domain/rendering/",
                "inktime/app/services/render",
                "inktime/app/services/display_prepare",
                "inktime/app/services/release",
                "inktime/app/domain/photopainter/",
            )
        ):
            domains.add("render_release")
            suites.add("render_release_owner")

        if path.startswith(("inktime/app/domain/photos/", "inktime/app/services/local_selection")) or any(
            token in lower_path for token in ("scanner", "incremental_scan", "photo_quality", "thumbnail")
        ):
            domains.add("scanner_photos")
            suites.add("scanner_photos_owner")

        if path.startswith(
            (
                "inktime/app/providers/",
                "inktime/app/domain/analysis/",
                "inktime/app/services/analysis",
                "inktime/app/services/provider",
                "inktime/app/services/scoring",
            )
        ):
            domains.add("provider_ai")
            suites.add("provider_analysis_owner")

        if path in {
            "inktime/app/services/model_benchmark.py",
            "inktime/app/services/benchmark_metrics.py",
        }:
            domains.add("benchmark")
            suites.add("benchmark_contract")
            gates.add("benchmark")

        if path.startswith(
            (
                "inktime/app/api/notifications",
                "inktime/app/services/notifications",
                "inktime/app/services/observability",
            )
        ) or any(token in lower_path for token in ("webhook", "logging")):
            domains.add("notifications_observability")
            suites.add("notifications_observability_owner")

        if path.startswith(
            ("inktime/app/api/settings", "inktime/app/repositories/settings", "inktime/app/services/settings")
        ):
            domains.add("settings_governance")
            suites.add("settings_governance_owner")

        return domains, suites, gates, False

    return {"unknown"}, set(), set(), True


def classify_paths(paths: Iterable[str]) -> dict[str, Any]:
    """Classify changed paths into canonical domains, suites, and gates."""

    normalised_paths: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = _normalise_path(raw_path)
        if path and path not in seen:
            normalised_paths.append(path)
            seen.add(path)

    domains: set[str] = set()
    production_domains: set[str] = set()
    suites: set[str] = set()
    gates: set[str] = set()
    unknown_paths: list[str] = []
    firmware_profiles: set[str] = set()
    firmware_profile_reasons: set[str] = set()

    for path in normalised_paths:
        path_domains, path_suites, path_gates, unknown = _classify_path(path)
        domains.update(path_domains)
        suites.update(path_suites)
        gates.update(path_gates)
        if "firmware" in path_domains:
            profiles, reason = _firmware_profiles_for_path(path)
            firmware_profiles.update(profiles)
            if reason:
                firmware_profile_reasons.add(reason)
        if not path.startswith("tests/"):
            production_domains.update(path_domains & PRODUCTION_DOMAINS)
        if unknown:
            unknown_paths.append(path)

    owner_suite_gaps: dict[str, list[str]] = {}
    for domain in _ordered(production_domains, DOMAIN_ORDER):
        owner_suites = DOMAIN_OWNER_SUITES.get(domain)
        if owner_suites is None or not set(owner_suites) & suites:
            owner_suite_gaps[domain] = list(owner_suites or ("conservative_full",))
    docs_only = bool(normalised_paths) and not production_domains and domains <= {"docs"}
    test_only = bool(normalised_paths) and not production_domains and "test_surface" in domains
    return {
        "changed_paths": normalised_paths,
        "domains": _ordered(domains, DOMAIN_ORDER),
        "production_domains": _ordered(production_domains, DOMAIN_ORDER),
        "unknown_paths": sorted(unknown_paths),
        "owner_suite_gaps": owner_suite_gaps,
        "selected_test_suites": _ordered(suites, TIER_0_SUITES + FULL_TEST_SUITES),
        "expensive_gates": _ordered(gates, FULL_EXPENSIVE_GATES),
        "affected_firmware_profiles": _ordered(firmware_profiles, FULL_FIRMWARE_PROFILES),
        "firmware_profile_reasons": sorted(firmware_profile_reasons),
        "docs_only": docs_only,
        "test_only": test_only,
        "has_production_change": bool(production_domains),
    }


def _mode_reasons(context: Mapping[str, object]) -> list[str]:
    reasons: list[str] = []
    event_name = str(context.get("event_name", "")).strip()
    ref = str(context.get("ref", context.get("github_ref", ""))).strip()
    labels = {label.casefold() for label in _labels(context)}
    draft = _optional_bool(context, "pull_request_draft", "draft", "is_draft")

    if event_name == "push" and ref == "refs/heads/main":
        reasons.append("main_push")
    if event_name == "workflow_dispatch" and _is_truthy(context.get("full_suite")):
        reasons.append("manual_full_suite")
    if _is_truthy(context.get("force_full")) or _is_truthy(context.get("force_full_suite")):
        reasons.append("explicit_full_request")
    if "full-ci" in labels:
        reasons.append("full_ci_label")
    if draft is False:
        reasons.append("ready_pr")
    if event_name == "pull_request" and draft is None:
        reasons.append("missing_pull_request_draft_state")
    return reasons


def resolve_ci_mode(event_context: Mapping[str, object] | None = None) -> str:
    """Resolve impact/full mode from GitHub event semantics."""

    context = event_context or {}
    return FULL_MODE if _mode_reasons(context) else IMPACT_MODE


def build_test_plan(
    paths: Iterable[str], event_context: Mapping[str, object] | None = None
) -> dict[str, Any]:
    """Return a deterministic, JSON-serializable hosted CI plan."""

    context = event_context or {}
    classification = classify_paths(paths)
    reasons = _mode_reasons(context)
    mode = FULL_MODE if reasons else IMPACT_MODE

    if classification["unknown_paths"]:
        mode = FULL_MODE
        reasons.append("unknown_production_or_config_path")

    if classification["owner_suite_gaps"]:
        mode = FULL_MODE
        reasons.append("production_owner_suite_missing")

    if mode == FULL_MODE:
        selected_suites = list(FULL_PLAN_SUITES)
        selected_gates = list(FULL_EXPENSIVE_GATES)
        if "ci_config" in classification["domains"]:
            selected_gates.append("actionlint")
        affected_firmware_profiles = list(FULL_FIRMWARE_PROFILES)
        firmware_profile_mode = "full"
        firmware_profile_reasons = sorted(
            set(classification["firmware_profile_reasons"]) | {"full_matrix"}
        )
    else:
        selected_suites = list(
            dict.fromkeys(
                [
                    *TIER_0_SUITES,
                    *classification["selected_test_suites"],
                ]
            )
        )
        selected_gates = list(classification["expensive_gates"])
        if classification["has_production_change"] and "python" in classification["domains"]:
            selected_suites.append("mypy")
        if any(
            domain in classification["domains"]
            for domain in ("dependencies", "dev_dependencies", "e2e_tooling")
        ):
            selected_suites.append("dependency_policy")
        if "ci_config" in classification["domains"]:
            selected_gates.append("actionlint")

        selected_gates = _ordered(selected_gates, FULL_EXPENSIVE_GATES + TIER_0_GATES)
        selected_suites = _ordered(selected_suites, TIER_0_SUITES + FULL_TEST_SUITES)
        affected_firmware_profiles = list(classification["affected_firmware_profiles"])
        firmware_profile_mode = (
            "affected" if affected_firmware_profiles else "not_applicable"
        )
        firmware_profile_reasons = list(classification["firmware_profile_reasons"])

    why_full_suite = reasons if mode == FULL_MODE else []
    selected_gates = _ordered(selected_gates, GATE_ORDER)
    skipped_gates = _ordered(ALL_FULL_GATES - set(selected_gates), FULL_EXPENSIVE_GATES)
    selected_gates_with_tier_zero = _ordered(
        (*TIER_0_GATES, *selected_gates),
        GATE_ORDER,
    )
    if mode == FULL_MODE:
        firmware_execution_profiles = {
            "quick": [],
            "affected": [],
            "full_matrix": list(FULL_FIRMWARE_PROFILES),
        }
    else:
        firmware_execution_profiles = {
            "quick": (
                [PRIMARY_FIRMWARE_PROFILE]
                if "firmware_quick" in selected_gates
                else []
            ),
            "affected": [
                profile
                for profile in affected_firmware_profiles
                if profile != PRIMARY_FIRMWARE_PROFILE
                and "firmware_affected" in selected_gates
            ],
            "full_matrix": [],
        }

    execution_map = FULL_GATE_EXECUTION if mode == FULL_MODE else IMPACT_GATE_EXECUTION
    selected_execution_ids = [
        execution_map.get(gate, f"{mode}:{gate}") for gate in selected_gates_with_tier_zero
    ]
    execution_id_duplicates = sorted(
        {
            execution_id
            for execution_id in selected_execution_ids
            if selected_execution_ids.count(execution_id) > 1
        }
    )
    impact_execution_ids = [
        execution_id
        for execution_id in selected_execution_ids
        if execution_id.startswith("impact:")
    ]
    full_execution_ids = [
        execution_id
        for execution_id in selected_execution_ids
        if execution_id.startswith("full:")
    ]
    execution_overlap = sorted(
        set(impact_execution_ids) & set(full_execution_ids)
    )
    suite_gate_name_collisions = sorted(
        set(selected_suites) & set(selected_gates_with_tier_zero)
    )
    suite_execution_gaps = sorted(
        set(selected_suites) - set(SUITE_EXECUTION_OWNERS)
    )
    selected_owner_suites = (
        []
        if mode == FULL_MODE
        else [
            suite
            for suite in selected_suites
            if SUITE_EXECUTION_OWNERS.get(suite) == SELECTED_SUITE_RUNNER
        ]
    )
    full_gate_set = set(FULL_EXPENSIVE_GATES) | set(TIER_0_GATES)
    if "ci_config" in classification["domains"]:
        full_gate_set.add("actionlint")

    return {
        "ci_mode": mode,
        "changed_paths": classification["changed_paths"],
        "changed_domains": classification["domains"],
        "production_domains": classification["production_domains"],
        "selected_test_suites": selected_suites,
        "selected_owner_suites": selected_owner_suites,
        "suite_execution_gaps": suite_execution_gaps,
        "expensive_gates": selected_gates,
        "selected_gates": selected_gates_with_tier_zero,
        "skipped_gates": skipped_gates,
        "unknown_paths": classification["unknown_paths"],
        "owner_suite_gaps": classification["owner_suite_gaps"],
        "why_full_suite": why_full_suite,
        "base_sha": _optional_text(context.get("base_sha")),
        "head_sha": _optional_text(context.get("head_sha")),
        "affected_firmware_profiles": affected_firmware_profiles,
        "firmware_profile_mode": firmware_profile_mode,
        "firmware_profile_reasons": firmware_profile_reasons,
        "firmware_execution_profiles": firmware_execution_profiles,
        "docs_only": classification["docs_only"],
        "test_only": classification["test_only"],
        "production_owner_invariant": (
            not classification["owner_suite_gaps"] or mode == FULL_MODE
        ),
        "full_plan_complete": mode == FULL_MODE
        and set(FULL_PLAN_SUITES) <= set(selected_suites)
        and not (set(FULL_PLAN_SUITES) - set(SUITE_EXECUTION_OWNERS))
        and not suite_execution_gaps
        and full_gate_set <= set(selected_gates_with_tier_zero)
        and not suite_gate_name_collisions,
        "selected_execution_ids": selected_execution_ids,
        "impact_execution_ids": impact_execution_ids,
        "full_execution_ids": full_execution_ids,
        "execution_equivalence": dict(IMPACT_TO_FULL_EXECUTION),
        "execution_id_duplicates": execution_id_duplicates,
        "execution_overlap": execution_overlap,
        "suite_gate_name_collisions": suite_gate_name_collisions,
        "no_heavy_impact_duplicates": (
            not execution_id_duplicates
            and not execution_overlap
            and not suite_gate_name_collisions
            and not (
                mode == FULL_MODE
                and set(selected_gates_with_tier_zero) & IMPACT_ONLY_GATES
            )
        ),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="repository-relative changed paths")
    parser.add_argument("--paths-file", help="read one repository-relative path per line")
    parser.add_argument("--event-name", default="")
    parser.add_argument("--ref", default="")
    parser.add_argument("--base-sha")
    parser.add_argument("--head-sha")
    parser.add_argument("--draft", choices=("true", "false"))
    parser.add_argument("--full-suite", action="store_true")
    parser.add_argument("--label", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    paths = list(args.paths)
    if args.paths_file:
        with open(args.paths_file, encoding="utf-8") as path_file:
            paths.extend(line.strip() for line in path_file if line.strip())

    context: dict[str, object] = {
        "event_name": args.event_name,
        "ref": args.ref,
        "base_sha": args.base_sha,
        "head_sha": args.head_sha,
        "full_suite": args.full_suite,
        "labels": args.label,
    }
    if args.draft is not None:
        context["draft"] = args.draft
    print(json.dumps(build_test_plan(paths, context), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
