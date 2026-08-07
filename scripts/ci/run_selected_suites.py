"""Execute planner-selected Tier 1/2 owner regression suites.

The planner owns suite selection and this module owns the executable test
mapping. Heavy gates remain in their dedicated workflow jobs; this runner must
never silently discard an unknown suite.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Iterable

# Make direct workflow invocation (`python scripts/ci/run_selected_suites.py`)
# resolve the repository's namespace package as well as module invocation.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ci.test_plan import SELECTED_SUITE_RUNNER, SUITE_EXECUTION_OWNERS

RUNNER_SUITE_TEST_PATHS: dict[str, tuple[str, ...]] = {
    "ci_planner_contracts": (
        "tests/unit/test_ci_changed_paths.py",
        "tests/unit/test_ci_test_plan.py",
        "tests/unit/test_ci_selected_suites.py",
    ),
    "ci_routing_contracts": (
        "tests/unit/test_ci_changed_paths.py",
        "tests/unit/test_ci_test_plan.py",
        "tests/unit/test_ci_selected_suites.py",
    ),
    "python_application_owner": ("tests/unit",),
    "web_ui_owner": ("tests/integration/test_management_ui.py",),
    "web_api_owner": ("tests/integration/test_management_ui.py",),
    "auth_security_owner": ("tests/security",),
    "runtime_scheduler_owner": (
        "tests/unit/test_runtime_concurrency.py",
        "tests/unit/test_runtime_config.py",
        "tests/integration/test_jobs.py",
        "tests/integration/test_scheduler.py",
        "tests/integration/test_worker_runner.py",
        "tests/integration/test_worker_timeout.py",
        "tests/integration/test_offline_schedule_runtime.py",
    ),
    "queue_resilience_owner": (
        "tests/unit/test_device_delivery_contract.py",
        "tests/unit/test_offline_retry_contract.py",
        "tests/unit/test_resilience_repository.py",
        "tests/integration/test_device_test_ack.py",
        "tests/integration/test_offline_queue_ack.py",
        "tests/integration/test_resilience_runtime.py",
    ),
    "persistence_owner": (
        "tests/unit/test_backups.py",
        "tests/integration/test_sqlite_concurrency.py",
    ),
    "device_api_contract_owner": (
        "tests/unit/test_device_delivery_contract.py",
        "tests/security/test_device_pairing.py",
        "tests/security/test_device_release_authorization.py",
        "tests/security/test_device_tokens.py",
        "tests/integration/test_device_notifications.py",
        "tests/integration/test_device_test_ack.py",
    ),
    "device_delivery_owner": (
        "tests/unit/test_device_delivery_contract.py",
        "tests/integration/test_offline_queue_ack.py",
        "tests/integration/test_device_test_ack.py",
    ),
    "render_release_owner": (
        "tests/unit/test_composition.py",
        "tests/unit/test_display_prepare_contract.py",
        "tests/unit/test_photo_renderer.py",
        "tests/unit/test_photopainter_stock.py",
        "tests/unit/test_releases.py",
        "tests/integration/test_release_recovery.py",
        "tests/integration/test_renderer_device_test.py",
    ),
    "scanner_photos_owner": (
        "tests/unit/test_local_selection.py",
        "tests/unit/test_photo_dates.py",
        "tests/unit/test_preprocessing.py",
        "tests/integration/test_incremental_scan.py",
        "tests/integration/test_safe_scanner.py",
    ),
    "notifications_observability_owner": (
        "tests/unit/test_observability.py",
        "tests/unit/test_webhook_retry.py",
        "tests/integration/test_device_notifications.py",
    ),
    "settings_governance_owner": (
        "tests/unit/test_settings_repository_governance.py",
        "tests/integration/test_settings_governance.py",
    ),
    "provider_analysis_owner": (
        "tests/unit/test_analysis_plan.py",
        "tests/unit/test_analysis_schema.py",
        "tests/unit/test_provider_batch.py",
        "tests/unit/test_provider_config.py",
        "tests/unit/test_provider_contracts.py",
        "tests/unit/test_provider_router.py",
        "tests/unit/test_scoring.py",
        "tests/unit/test_scoring_rules.py",
    ),
    "unit_owner": ("tests/unit",),
    "integration_owner": ("tests/integration",),
}


def _ordered_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def selected_runner_suites(selected_suites: Iterable[str]) -> list[str]:
    selected = list(selected_suites)
    unknown = sorted(set(selected) - set(SUITE_EXECUTION_OWNERS))
    if unknown:
        raise ValueError(f"Unknown planner suite(s): {', '.join(unknown)}")

    runner_suites = [
        suite
        for suite in selected
        if SUITE_EXECUTION_OWNERS[suite] == SELECTED_SUITE_RUNNER
    ]
    missing = sorted(set(runner_suites) - set(RUNNER_SUITE_TEST_PATHS))
    if missing:
        raise ValueError(f"Missing executable mapping for suite(s): {', '.join(missing)}")
    return _ordered_unique(runner_suites)


def selected_test_paths(selected_suites: Iterable[str]) -> tuple[list[str], list[str]]:
    runner_suites = selected_runner_suites(selected_suites)
    paths = _ordered_unique(
        path
        for suite in runner_suites
        for path in RUNNER_SUITE_TEST_PATHS[suite]
    )
    return runner_suites, paths


def _parse_suites(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("--suites-json must be valid JSON") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("--suites-json must contain a JSON array of strings")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suites-json", required=True)
    args = parser.parse_args()

    try:
        selected_suites = _parse_suites(args.suites_json)
        runner_suites, test_paths = selected_test_paths(selected_suites)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not runner_suites:
        print("No Tier 1/2 owner regression suites selected; dedicated gates own the rest.")
        return 0

    missing_paths = [path for path in test_paths if not Path(path).exists()]
    if missing_paths:
        print(
            "ERROR: executable suite mapping references missing path(s): "
            + ", ".join(missing_paths),
            file=sys.stderr,
        )
        return 2

    print(f"Selected owner suites: {', '.join(runner_suites)}")
    print(f"Selected pytest paths: {', '.join(test_paths)}")
    return subprocess.run([sys.executable, "-m", "pytest", *test_paths], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
