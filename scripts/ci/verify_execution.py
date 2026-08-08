"""Fail-closed verification of planner-selected GitHub job execution."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
from pathlib import Path
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ci.provenance import provenance_errors
from scripts.ci.test_plan import (
    FULL_SUITE_EXECUTION_OWNERS,
    GATE_EXECUTION_OWNERS,
    NON_EXECUTABLE_SUITES,
    SUITE_EXECUTION_OWNER_JOB_IDS,
    SUITE_EXECUTION_OWNERS,
    WORKFLOW_EXECUTION_JOB_IDS,
)

WORKFLOW_ALIASES = {
    "ci": "ci",
    "container": "container",
    "container-security": "container",
}
AGGREGATE_JOBS = {
    "ci": "repository-gate",
    "container": "container-security-gate",
}
ALL_EXECUTION_JOB_IDS = frozenset(
    job_id for jobs in WORKFLOW_EXECUTION_JOB_IDS.values() for job_id in jobs
)


def _execution_job_id(owner: str) -> str:
    return SUITE_EXECUTION_OWNER_JOB_IDS.get(owner, owner)


def _error(
    execution_id: str,
    job_name: str,
    expected_result: str,
    actual_result: str,
    detail: str = "",
) -> str:
    suffix = f": {detail}" if detail else ""
    return (
        f"execution_id={execution_id} job_name={job_name} "
        f"expected_result={expected_result} actual_result={actual_result}{suffix}"
    )


def _result(value: object) -> str:
    if isinstance(value, Mapping):
        raw_result = value.get("result")
    else:
        raw_result = None
    return str(raw_result) if raw_result is not None else "unknown"


def expected_execution_jobs(plan: Mapping[str, Any], workflow: str) -> dict[str, str]:
    """Return selected execution job IDs for one aggregate workflow."""

    canonical_workflow = WORKFLOW_ALIASES.get(workflow)
    if canonical_workflow is None:
        raise ValueError(f"unknown workflow identity: {workflow}")

    selected_suites = plan.get("selected_test_suites", [])
    selected_gates = plan.get("selected_gates", [])
    if not isinstance(selected_suites, list) or not all(
        isinstance(value, str) for value in selected_suites
    ):
        raise ValueError("selected_test_suites must be a JSON array of strings")
    if not isinstance(selected_gates, list) or not all(
        isinstance(value, str) for value in selected_gates
    ):
        raise ValueError("selected_gates must be a JSON array of strings")

    mode = plan.get("ci_mode")
    suite_registry = (
        FULL_SUITE_EXECUTION_OWNERS
        if mode == "full"
        else SUITE_EXECUTION_OWNERS
    )
    workflow_jobs = WORKFLOW_EXECUTION_JOB_IDS[canonical_workflow]
    aggregate_job = AGGREGATE_JOBS[canonical_workflow]
    expected: dict[str, str] = {"changes": "canonical planner"}

    if plan.get("event_name") == "pull_request":
        expected["source-head-validation"] = "source-head provenance"

    for suite in selected_suites:
        if suite in NON_EXECUTABLE_SUITES:
            continue
        owner = suite_registry.get(suite)
        if owner is None:
            continue
        job_id = _execution_job_id(owner)
        if job_id in workflow_jobs and job_id != aggregate_job:
            expected.setdefault(job_id, f"suite:{suite}")

    for gate in selected_gates:
        owner = GATE_EXECUTION_OWNERS.get(gate)
        if owner in workflow_jobs and owner != aggregate_job:
            expected.setdefault(owner, f"gate:{gate}")

    return expected


def _validate_planner_registry(
    plan: Mapping[str, Any], workflow: str
) -> list[str]:
    errors: list[str] = []
    canonical_workflow = WORKFLOW_ALIASES.get(workflow)
    if canonical_workflow is None:
        return [
            _error(
                f"workflow:{workflow}",
                workflow,
                "known workflow identity",
                "unknown",
            )
        ]

    mode = plan.get("ci_mode")
    suite_registry = (
        FULL_SUITE_EXECUTION_OWNERS
        if mode == "full"
        else SUITE_EXECUTION_OWNERS
    )
    for suite in plan.get("selected_test_suites", []):
        if suite in NON_EXECUTABLE_SUITES:
            continue
        owner = suite_registry.get(suite)
        if owner is None:
            errors.append(
                _error(
                    f"suite:{suite}",
                    suite,
                    "registered execution owner",
                    "missing",
                    "planner selected a suite without an execution owner",
                )
            )
        elif _execution_job_id(owner) not in ALL_EXECUTION_JOB_IDS:
            errors.append(
                _error(
                    f"suite:{suite}",
                    _execution_job_id(owner),
                    "known execution job",
                    "unknown",
                    "suite registry points outside the execution registry",
                )
            )

    for gate in plan.get("selected_gates", []):
        owner = GATE_EXECUTION_OWNERS.get(gate)
        if owner is None:
            errors.append(
                _error(
                    f"gate:{gate}",
                    gate,
                    "registered execution owner",
                    "missing",
                    "planner selected a gate without an execution owner",
                )
            )
        elif owner not in ALL_EXECUTION_JOB_IDS:
            errors.append(
                _error(
                    f"gate:{gate}",
                    owner,
                    "known execution job",
                    "unknown",
                    "gate registry points outside the execution registry",
                )
            )
    return errors


def verify_execution(
    plan: Mapping[str, Any], needs: Mapping[str, Any], workflow: str
) -> list[str]:
    """Return fail-closed execution errors for a canonical plan and needs map."""

    canonical_workflow = WORKFLOW_ALIASES.get(workflow)
    if canonical_workflow is None:
        return _validate_planner_registry(plan, workflow)

    errors = _validate_planner_registry(plan, canonical_workflow)
    for provenance_error in provenance_errors(
        {
            key: str(plan.get(key) or "")
            for key in (
                "event_name",
                "ref",
                "source_head_sha",
                "base_sha",
                "tested_sha",
                "tested_ref_kind",
            )
        }
    ):
        errors.append(
            _error(
                "provenance",
                "changes",
                "valid canonical provenance",
                "invalid",
                provenance_error,
            )
        )

    known_needs_jobs = set(WORKFLOW_EXECUTION_JOB_IDS[canonical_workflow])
    known_needs_jobs.discard(AGGREGATE_JOBS[canonical_workflow])
    for job_name in sorted(set(needs) - known_needs_jobs):
        errors.append(
            _error(
                f"unknown:{job_name}",
                job_name,
                "known execution dependency",
                "unknown",
                "needs contains an execution not declared by the source registry",
            )
        )

    try:
        expected = expected_execution_jobs(plan, canonical_workflow)
    except ValueError as exc:
        errors.append(
            _error(
                "planner",
                "changes",
                "valid canonical planner JSON",
                "invalid",
                str(exc),
            )
        )
        return errors

    for job_name, execution_id in sorted(expected.items()):
        if job_name not in needs:
            errors.append(
                _error(execution_id, job_name, "success", "missing")
            )
            continue
        actual = _result(needs[job_name])
        if actual != "success":
            errors.append(
                _error(execution_id, job_name, "success", actual)
            )

    for job_name in sorted(set(needs) - set(expected) - {AGGREGATE_JOBS[canonical_workflow]}):
        actual = _result(needs[job_name])
        if actual not in {"success", "skipped"}:
            errors.append(
                _error(
                    f"unselected:{job_name}",
                    job_name,
                    "success-or-skipped",
                    actual,
                    "unselected execution must not fail or cancel the aggregate",
                )
            )
    return errors


def _parse_json(raw: str, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--needs-json", required=True)
    parser.add_argument("--workflow", required=True, choices=sorted(WORKFLOW_ALIASES))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        plan = _parse_json(args.plan_json, "--plan-json")
        needs = _parse_json(args.needs_json, "--needs-json")
    except ValueError as exc:
        print(
            _error("input", args.workflow, "valid JSON objects", "invalid", str(exc))
        )
        return 1

    errors = verify_execution(plan, needs, args.workflow)
    if errors:
        print("Execution verification FAILED")
        print("\n".join(errors))
        return 1
    print(f"Execution verification PASS: workflow={WORKFLOW_ALIASES[args.workflow]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
