"""Fail-closed attestation for planner-selected GitHub Actions execution.

The planner owns what must run. This module owns the source-level mapping from
selected suites/gates to workflow jobs, so aggregate YAML only supplies the
canonical plan and ``toJSON(needs)``.
"""

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
    FULL_MODE,
    FULL_SUITE_EXECUTION_OWNERS,
    GATE_EXECUTION_OWNERS,
    NON_EXECUTABLE_SUITES,
    SUITE_EXECUTION_OWNER_JOB_IDS,
    SUITE_EXECUTION_OWNERS,
    WORKFLOW_EXECUTION_JOB_IDS,
)

REPOSITORY_WORKFLOW = "repository"
CONTAINER_WORKFLOW = "container"
WORKFLOW_IDENTITIES = frozenset({REPOSITORY_WORKFLOW, CONTAINER_WORKFLOW})
WORKFLOW_ALIASES = {"ci": REPOSITORY_WORKFLOW, "repository": REPOSITORY_WORKFLOW, "container": CONTAINER_WORKFLOW}
AGGREGATE_JOB_BY_WORKFLOW = {
    REPOSITORY_WORKFLOW: "repository-gate",
    CONTAINER_WORKFLOW: "container-security-gate",
}


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


def _workflow_jobs(workflow: str) -> frozenset[str]:
    registry_key = "ci" if workflow == REPOSITORY_WORKFLOW else workflow
    return WORKFLOW_EXECUTION_JOB_IDS[registry_key]


def _job_workflow(job_id: str, current_workflow: str | None = None) -> str | None:
    if job_id == "changes":
        return current_workflow
    if job_id == "source-head-contract":
        return REPOSITORY_WORKFLOW
    if job_id in WORKFLOW_EXECUTION_JOB_IDS["ci"]:
        return REPOSITORY_WORKFLOW
    if job_id in WORKFLOW_EXECUTION_JOB_IDS["container"]:
        return CONTAINER_WORKFLOW
    return None


def _workflow_job_id(owner_id: str) -> str:
    return SUITE_EXECUTION_OWNER_JOB_IDS.get(owner_id, owner_id)


def _string_list(plan: Mapping[str, object], key: str) -> tuple[list[str], list[str]]:
    raw = plan.get(key)
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        return [], [
            _error(
                f"plan.{key}",
                "changes",
                "JSON array of strings",
                "invalid",
                f"plan.{key} must be a JSON array of strings",
            )
        ]
    return list(raw), []


def expected_execution_jobs(
    plan: Mapping[str, object], workflow_identity: str
) -> tuple[list[str], list[str]]:
    """Return jobs that must be ``success`` plus source-registry errors."""

    workflow = WORKFLOW_ALIASES.get(workflow_identity)
    if workflow is None:
        return [], [
            _error(
                f"workflow:{workflow_identity}",
                workflow_identity,
                "known workflow identity",
                "unknown",
            )
        ]

    errors: list[str] = []
    mode = plan.get("ci_mode")
    if mode not in {"impact", FULL_MODE}:
        errors.append(
            _error("planner", "changes", "impact-or-full", str(mode), "unknown ci_mode")
        )
        return [], errors
    if mode == FULL_MODE and plan.get("full_plan_complete") is not True:
        errors.append(
            _error(
                "planner",
                "changes",
                "full_plan_complete=true",
                str(plan.get("full_plan_complete")),
            )
        )

    selected_suites, suite_errors = _string_list(plan, "selected_test_suites")
    selected_gates, gate_errors = _string_list(plan, "selected_gates")
    errors.extend(suite_errors)
    errors.extend(gate_errors)
    suite_registry = (
        FULL_SUITE_EXECUTION_OWNERS if mode == FULL_MODE else SUITE_EXECUTION_OWNERS
    )
    expected: set[str] = {"changes"}

    requires_source = plan.get("requires_source_head_contract")
    if requires_source is True and workflow == REPOSITORY_WORKFLOW:
        expected.add("source-head-contract")
    elif plan.get("event_name") == "pull_request" and "requires_source_head_contract" not in plan:
        errors.append(
            _error(
                "provenance",
                "source-head-contract",
                "requires_source_head_contract=true",
                "missing",
            )
        )

    current_aggregate = AGGREGATE_JOB_BY_WORKFLOW[workflow]
    for suite in selected_suites:
        if suite in NON_EXECUTABLE_SUITES:
            continue
        owner_id = suite_registry.get(suite)
        if owner_id is None:
            errors.append(
                _error(
                    f"suite:{suite}",
                    suite,
                    "registered execution owner",
                    "missing",
                    "selected suite has no execution owner",
                )
            )
            continue
        job_id = _workflow_job_id(owner_id)
        owner_workflow = _job_workflow(job_id, workflow)
        if owner_workflow is None:
            errors.append(
                _error(
                    f"suite:{suite}",
                    job_id,
                    "known execution job",
                    "unknown",
                    "suite registry points outside the execution registry",
                )
            )
        elif owner_workflow == workflow and job_id != current_aggregate:
            expected.add(job_id)

    for gate in selected_gates:
        job_id = GATE_EXECUTION_OWNERS.get(gate)
        if job_id is None:
            errors.append(
                _error(
                    f"gate:{gate}",
                    gate,
                    "registered execution owner",
                    "missing",
                    "selected gate has no execution owner",
                )
            )
            continue
        owner_workflow = _job_workflow(job_id, workflow)
        if gate in {"repository_gate", "container_security_gate"}:
            owner_workflow = workflow if job_id == current_aggregate else owner_workflow
        if owner_workflow is None:
            errors.append(
                _error(
                    f"gate:{gate}",
                    job_id,
                    "known execution job",
                    "unknown",
                    "gate registry points outside the execution registry",
                )
            )
        elif owner_workflow == workflow and job_id != current_aggregate:
            expected.add(job_id)

    return sorted(expected), errors


def _result(value: object) -> str:
    if isinstance(value, Mapping):
        raw = value.get("result")
        return str(raw) if raw is not None else "unknown"
    return "unknown"


def _execution_id_for_job(
    plan: Mapping[str, object], workflow: str, job_id: str
) -> str:
    if job_id == "changes":
        return "changes"
    if job_id == "source-head-contract":
        return "source-head-contract"
    mode = plan.get("ci_mode")
    suite_registry = (
        FULL_SUITE_EXECUTION_OWNERS if mode == FULL_MODE else SUITE_EXECUTION_OWNERS
    )
    suites = plan.get("selected_test_suites")
    if isinstance(suites, list):
        for suite in suites:
            if not isinstance(suite, str) or suite in NON_EXECUTABLE_SUITES:
                continue
            owner_id = suite_registry.get(suite)
            if owner_id is None:
                continue
            owner_job = _workflow_job_id(owner_id)
            if owner_job == job_id and _job_workflow(owner_job, workflow) == workflow:
                return f"suite:{suite}"
    gates = plan.get("selected_gates")
    if isinstance(gates, list):
        for gate in gates:
            if not isinstance(gate, str):
                continue
            owner_job = GATE_EXECUTION_OWNERS.get(gate)
            if owner_job == job_id:
                return f"gate:{gate}"
    return job_id


def verify_execution(
    plan: Mapping[str, object],
    needs: Mapping[str, object],
    workflow_identity: str,
) -> list[str]:
    """Return fail-closed attestation errors; an empty list means PASS."""

    workflow = WORKFLOW_ALIASES.get(workflow_identity)
    expected_jobs, errors = expected_execution_jobs(plan, workflow_identity)
    if workflow is None:
        return errors

    provenance_values = {
        key: str(plan.get(key) or "")
        for key in (
            "event_name",
            "ref",
            "source_head_sha",
            "base_sha",
            "tested_sha",
            "tested_ref",
            "tested_ref_kind",
        )
    }
    for provenance_error in provenance_errors(provenance_values):
        errors.append(
            _error(
                "provenance",
                "changes",
                "valid canonical provenance",
                "invalid",
                provenance_error,
            )
        )

    known_needs = set(_workflow_jobs(workflow))
    known_needs.discard(AGGREGATE_JOB_BY_WORKFLOW[workflow])
    for job_name in sorted(set(needs) - known_needs):
        errors.append(
            _error(
                f"unknown:{job_name}",
                job_name,
                "known execution dependency",
                "unknown",
                "needs contains an execution not declared by the source registry",
            )
        )

    for job_id in expected_jobs:
        execution_id = _execution_id_for_job(plan, workflow, job_id)
        if job_id not in needs:
            errors.append(_error(execution_id, job_id, "success", "missing"))
            continue
        actual = _result(needs[job_id])
        if actual != "success":
            errors.append(_error(execution_id, job_id, "success", actual))

    for job_name in sorted(set(needs) - set(expected_jobs) - {AGGREGATE_JOB_BY_WORKFLOW[workflow]}):
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


def _json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--needs-json", required=True)
    parser.add_argument("--workflow", required=True, choices=["ci", "repository", "container"])
    args = parser.parse_args()
    try:
        plan = _json_object(args.plan_json, "--plan-json")
        needs = _json_object(args.needs_json, "--needs-json")
    except ValueError as exc:
        print(_error("input", args.workflow, "valid JSON objects", "invalid", str(exc)))
        return 2

    errors = verify_execution(plan, needs, args.workflow)
    if errors:
        print("execution attestation: FAIL")
        print("\n".join(errors))
        return 1
    expected, _ = expected_execution_jobs(plan, args.workflow)
    print(
        "execution attestation: PASS: "
        + (", ".join(expected) if expected else "no predecessor jobs selected")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
