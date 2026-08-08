"""Execute planner-selected Tier 1/2 owner regression suites.

The planner owns suite selection and this module owns the executable test
mapping. Heavy gates remain in their dedicated workflow jobs; this runner must
never silently discard an unknown suite.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
import json
import subprocess
import sys
from pathlib import Path

# Make direct workflow invocation (`python scripts/ci/run_selected_suites.py`)
# resolve the repository's namespace package as well as module invocation.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ci.test_plan import (
    FULL_ONLY_TEST_PATH_REASONS,
    NON_EXECUTABLE_SUITES,
    SELECTED_SUITE_RUNNER,
    SUITE_EXECUTION_OWNERS,
)

RUNNER_SUITE_TEST_PATHS: dict[str, tuple[str, ...]] = {
    "ci_planner_contracts": (
        "tests/unit/test_ci_changed_paths.py",
        "tests/unit/test_ci_test_plan.py",
        "tests/unit/test_ci_selected_suites.py",
        "tests/unit/test_ci_execution_attestation.py",
        "tests/unit/test_ci_planner_hardening.py",
    ),
    "ci_routing_contracts": (
        "tests/unit/test_ci_changed_paths.py",
        "tests/unit/test_ci_test_plan.py",
        "tests/unit/test_ci_selected_suites.py",
        "tests/unit/test_ci_execution_attestation.py",
        "tests/unit/test_ci_planner_hardening.py",
    ),
    "python_application_owner": (
        "tests/unit",
        "tests/integration/test_application_factory.py",
    ),
    "web_ui_owner": ("tests/integration/test_management_ui.py",),
    "web_api_owner": (
        "tests/integration/test_management_ui.py",
        "tests/integration/test_activity_console.py",
        "tests/integration/test_deployment_modes.py",
        "tests/integration/test_photopainter_stock_api.py",
        "tests/integration/test_review_workbench.py",
    ),
    "auth_security_owner": (
        "tests/security",
        "tests/integration/test_deployment_modes.py",
    ),
    "runtime_scheduler_owner": (
        "tests/unit/test_runtime_concurrency.py",
        "tests/unit/test_runtime_config.py",
        "tests/integration/test_jobs.py",
        "tests/integration/test_scheduler.py",
        "tests/integration/test_worker_runner.py",
        "tests/integration/test_worker_timeout.py",
        "tests/integration/test_offline_schedule_runtime.py",
        "tests/integration/test_application_factory.py",
        "tests/integration/test_runtime_soak_cli.py",
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
        "tests/integration/test_final_review_history.py",
    ),
    "device_api_contract_owner": (
        "tests/unit/test_device_delivery_contract.py",
        "tests/security/test_device_pairing.py",
        "tests/security/test_device_release_authorization.py",
        "tests/security/test_device_tokens.py",
        "tests/integration/test_device_notifications.py",
        "tests/integration/test_device_test_ack.py",
        "tests/integration/test_adaptive_frame_devices.py",
        "tests/integration/test_photopainter_stock_api.py",
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
        "tests/integration/test_adaptive_frame_renderer.py",
        "tests/integration/test_dual_photo_caption_layout.py",
        "tests/integration/test_render_candidate_contract.py",
    ),
    "scanner_photos_owner": (
        "tests/unit/test_local_selection.py",
        "tests/unit/test_photo_dates.py",
        "tests/unit/test_preprocessing.py",
        "tests/integration/test_incremental_scan.py",
        "tests/integration/test_safe_scanner.py",
        "tests/integration/test_photo_quality_ai.py",
    ),
    "notifications_observability_owner": (
        "tests/unit/test_observability.py",
        "tests/unit/test_webhook_retry.py",
        "tests/integration/test_device_notifications.py",
        "tests/integration/test_activity_console.py",
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
        "tests/integration/test_ai_cache_singleflight.py",
        "tests/integration/test_analysis_pipeline.py",
        "tests/integration/test_local_only_mode.py",
        "tests/integration/test_photo_quality_ai.py",
        "tests/integration/test_review_workbench.py",
    ),
    "backup_restore_owner": ("tests/unit/test_backups.py",),
    "unit_owner": ("tests/unit",),
}

# Integration tests omitted from a focused owner mapping must have an explicit
# full-only reason before they are intentionally treated that way.  The current
# hardening maps all known high-value provider/render cross-layer regressions,
# so this allowlist starts empty rather than silently blessing omissions.
FULL_ONLY_INTEGRATION_TESTS: dict[str, str] = dict(FULL_ONLY_TEST_PATH_REASONS)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _test_target_errors(raw_path: str, repository_root: Path) -> list[str]:
    path = Path(raw_path)
    if path.is_absolute():
        return [f"{raw_path}: mapping must be repository-relative"]

    root = repository_root.resolve()
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return [f"{raw_path}: mapping escapes repository root"]

    if not target.exists():
        return [f"{raw_path}: path does not exist"]
    if target.is_file():
        errors = []
        if target.suffix != ".py":
            errors.append(f"{raw_path}: mapped file must be a .py test file")
        if not target.name.startswith("test_"):
            errors.append(f"{raw_path}: mapped file must start with test_")
        return errors
    if target.is_dir():
        if not any(
            candidate.is_file() and candidate.name.startswith("test_") and candidate.suffix == ".py"
            for candidate in target.rglob("test_*.py")
        ):
            return [f"{raw_path}: mapped directory contains no test_*.py"]
        return []
    return [f"{raw_path}: mapped path is neither a file nor a directory"]


def validate_runner_test_paths(
    paths: Iterable[str], *, repository_root: Path = REPOSITORY_ROOT
) -> list[str]:
    """Return deterministic validation errors for repository-owned pytest targets."""

    errors: list[str] = []
    for path in paths:
        errors.extend(_test_target_errors(path, repository_root))
    return errors


def validate_runner_suite_test_paths(
    mapping: Mapping[str, Iterable[str]] = RUNNER_SUITE_TEST_PATHS,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> list[str]:
    """Validate every source-owned runner mapping against the repository root."""

    errors: list[str] = []
    for suite in sorted(mapping):
        for path in mapping[suite]:
            errors.extend(f"{suite}: {error}" for error in _test_target_errors(path, repository_root))
    for path, reason in sorted(FULL_ONLY_INTEGRATION_TESTS.items()):
        errors.extend(
            f"full-only integration: {error}"
            for error in _test_target_errors(path, repository_root)
        )
        if not reason.strip():
            errors.append(f"full-only integration: {path}: reason must not be empty")
    return errors


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
    known_suites = set(SUITE_EXECUTION_OWNERS) | NON_EXECUTABLE_SUITES
    unknown = sorted(set(selected) - known_suites)
    if unknown:
        raise ValueError(f"Unknown planner suite(s): {', '.join(unknown)}")

    runner_suites = [
        suite
        for suite in selected
        if suite not in NON_EXECUTABLE_SUITES
        and SUITE_EXECUTION_OWNERS[suite] == SELECTED_SUITE_RUNNER
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

    mapping_errors = validate_runner_suite_test_paths()
    if mapping_errors:
        print(
            "ERROR: runner suite mapping is invalid: " + "; ".join(mapping_errors),
            file=sys.stderr,
        )
        return 2

    if not runner_suites:
        print("No Tier 1/2 owner regression suites selected; dedicated gates own the rest.")
        return 0

    path_errors = validate_runner_test_paths(test_paths)
    if path_errors:
        print(
            "ERROR: executable suite mapping references invalid path(s): "
            + "; ".join(path_errors),
            file=sys.stderr,
        )
        return 2

    print(f"Selected owner suites: {', '.join(runner_suites)}")
    print(f"Selected pytest paths: {', '.join(test_paths)}")
    # Every pytest path comes from the source-owned mapping and was validated above.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", *test_paths], check=False
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
