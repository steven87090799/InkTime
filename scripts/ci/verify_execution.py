"""Fail-closed attestation for planner-selected GitHub Actions execution.

The planner owns *what* must run.  This module owns the source-level contract
that maps selected suites/gates to the workflow job that must actually execute
them.  Aggregate jobs pass ``toJSON(needs)`` here and therefore do not need a
second routing policy in YAML.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ci.test_plan import (
    FULL_MODE,
    FULL_SUITE_EXECUTION_OWNERS,
    NON_EXECUTABLE_SUITES,
    SELECTED_SUITE_RUNNER,
    SUITE_EXECUTION_OWNERS,
)

REPOSITORY_WORKFLOW = "repository"
CONTAINER_WORKFLOW = "container"
WORKFLOW_IDENTITIES = frozenset({REPOSITORY_WORKFLOW, CONTAINER_WORKFLOW})

# ``changes`` exists independently in both workflows. Every other execution
# owner is globally unique and therefore has one canonical workflow owner.
WORKFLOW_LOCAL_JOBS = frozenset({"changes"})
JOB_WORKFLOW = {
    "source-head-contract": REPOSITORY_WORKFLOW,
    "python-quality": REPOSITORY_WORKFLOW,
    "python-compatibility": REPOSITORY_WORKFLOW,
    "dependency-audit": REPOSITORY_WORKFLOW,
    "migration-contract": REPOSITORY_WORKFLOW,
    "secret-scan": REPOSITORY_WORKFLOW,
    "actionlint": REPOSITORY_WORKFLOW,
    SELECTED_SUITE_RUNNER: REPOSITORY_WORKFLOW,
    "compose-lan-production-persistence": REPOSITORY_WORKFLOW,
    "compose-production-tls-smoke": REPOSITORY_WORKFLOW,
    "bounded-runtime-soak": REPOSITORY_WORKFLOW,
    "playwright": REPOSITORY_WORKFLOW,
    "firmware-host-contract": REPOSITORY_WORKFLOW,
    "esp32-compile": REPOSITORY_WORKFLOW,
    "container-security": CONTAINER_WORKFLOW,
    "benchmark-contract": CONTAINER_WORKFLOW,
}

# Planner execution-owner names are logical source identifiers. Most equal the
# GitHub job id; the selected-suite runner predates this contract and uses an
# underscore logically while the workflow job id uses a hyphen.
WORKFLOW_JOB_ID = {
    SELECTED_SUITE_RUNNER: "selected-owner-suites",
}

# Gate -> (workflow identity, workflow job id). Aggregate gates point at their
# own current job and are considered attested by reaching this verifier; they
# cannot appear inside their own ``needs`` object.
GATE_EXECUTION_OWNERS = {
    "secret_scan": (REPOSITORY_WORKFLOW, "secret-scan"),
    "actionlint": (REPOSITORY_WORKFLOW, "actionlint"),
    "python312_full": (REPOSITORY_WORKFLOW, "python-quality"),
    "python310_compatibility": (REPOSITORY_WORKFLOW, "python-compatibility"),
    "dependency_audit": (REPOSITORY_WORKFLOW, "dependency-audit"),
    "migration": (REPOSITORY_WORKFLOW, "migration-contract"),
    "runtime_soak": (REPOSITORY_WORKFLOW, "bounded-runtime-soak"),
    "playwright": (REPOSITORY_WORKFLOW, "playwright"),
    "docker_lan_persistence": (REPOSITORY_WORKFLOW, "compose-lan-production-persistence"),
    "tls_smoke": (REPOSITORY_WORKFLOW, "compose-production-tls-smoke"),
    "firmware_host_contract": (REPOSITORY_WORKFLOW, "firmware-host-contract"),
    "firmware_quick": (REPOSITORY_WORKFLOW, "esp32-compile"),
    "firmware_affected": (REPOSITORY_WORKFLOW, "esp32-compile"),
    "firmware_full_matrix": (REPOSITORY_WORKFLOW, "esp32-compile"),
    "container_security": (CONTAINER_WORKFLOW, "container-security"),
    "benchmark": (CONTAINER_WORKFLOW, "benchmark-contract"),
    "repository_gate": (REPOSITORY_WORKFLOW, "repository-gate"),
    "container_security_gate": (CONTAINER_WORKFLOW, "container-security-gate"),
}

AGGREGATE_JOB_BY_WORKFLOW = {
    REPOSITORY_WORKFLOW: "repository-gate",
    CONTAINER_WORKFLOW: "container-security-gate",
}


def _string_list(plan: Mapping[str, object], key: str) -> tuple[list[str], list[str]]:
    raw = plan.get(key)
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        return [], [f"plan.{key} must be a JSON array of strings"]
    return list(raw), []


def _job_workflow(owner_id: str, workflow_identity: str) -> str | None:
    if owner_id in WORKFLOW_LOCAL_JOBS:
        return workflow_identity
    return JOB_WORKFLOW.get(owner_id)


def _workflow_job_id(owner_id: str) -> str:
    return WORKFLOW_JOB_ID.get(owner_id, owner_id)


def expected_execution_jobs(
    plan: Mapping[str, object], workflow_identity: str
) -> tuple[list[str], list[str]]:
    """Return jobs that must be ``success`` in this workflow plus contract errors."""

    errors: list[str] = []
    if workflow_identity not in WORKFLOW_IDENTITIES:
        return [], [f"unknown workflow identity: {workflow_identity}"]

    mode = plan.get("ci_mode")
    if mode not in {"impact", FULL_MODE}:
        errors.append(f"unknown ci_mode: {mode!r}")
        return [], errors
    if mode == FULL_MODE and plan.get("full_plan_complete") is not True:
        errors.append("full mode requires plan.full_plan_complete=true")

    selected_suites, suite_errors = _string_list(plan, "selected_test_suites")
    selected_gates, gate_errors = _string_list(plan, "selected_gates")
    errors.extend(suite_errors)
    errors.extend(gate_errors)

    suite_registry = (
        FULL_SUITE_EXECUTION_OWNERS if mode == FULL_MODE else SUITE_EXECUTION_OWNERS
    )
    expected: set[str] = set()

    for suite in selected_suites:
        if suite in NON_EXECUTABLE_SUITES:
            continue
        owner_id = suite_registry.get(suite)
        if owner_id is None:
            errors.append(f"selected suite has no execution owner: {suite}")
            continue
        owner_workflow = _job_workflow(owner_id, workflow_identity)
        if owner_workflow is None:
            errors.append(
                f"selected suite execution owner is unknown: {suite} -> {owner_id}"
            )
            continue
        if owner_workflow == workflow_identity:
            expected.add(_workflow_job_id(owner_id))

    current_aggregate = AGGREGATE_JOB_BY_WORKFLOW[workflow_identity]
    for gate in selected_gates:
        owner = GATE_EXECUTION_OWNERS.get(gate)
        if owner is None:
            errors.append(f"selected gate has no execution owner: {gate}")
            continue
        owner_workflow, owner_job = owner
        if owner_workflow not in WORKFLOW_IDENTITIES:
            errors.append(
                f"selected gate execution workflow is unknown: {gate} -> {owner_workflow}"
            )
            continue
        if owner_workflow == workflow_identity and owner_job != current_aggregate:
            expected.add(owner_job)

    # Pull-request source-head provenance is deliberately lightweight and is
    # separate from merge-ref validation, but it is still fail-closed once the
    # planner marks it required.
    if plan.get("requires_source_head_contract") is True:
        if workflow_identity == REPOSITORY_WORKFLOW:
            expected.add("source-head-contract")
    elif plan.get("event_name") == "pull_request" and "requires_source_head_contract" not in plan:
        errors.append("pull_request plan is missing requires_source_head_contract")

    return sorted(expected), errors


def verify_execution(
    plan: Mapping[str, object],
    needs: Mapping[str, object],
    workflow_identity: str,
) -> list[str]:
    """Return fail-closed attestation errors; an empty list means PASS."""

    expected_jobs, errors = expected_execution_jobs(plan, workflow_identity)
    for job_id in expected_jobs:
        raw_job = needs.get(job_id)
        if not isinstance(raw_job, Mapping):
            errors.append(f"selected execution missing from needs: {job_id}")
            continue
        result = raw_job.get("result")
        if result != "success":
            errors.append(
                f"selected execution must be success: {job_id} result={result!r}"
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
    parser.add_argument("--workflow", required=True, choices=sorted(WORKFLOW_IDENTITIES))
    args = parser.parse_args()

    try:
        plan = _json_object(args.plan_json, "--plan-json")
        needs = _json_object(args.needs_json, "--needs-json")
    except ValueError as exc:
        print(f"execution attestation: FAIL: {exc}", file=sys.stderr)
        return 2

    errors = verify_execution(plan, needs, args.workflow)
    if errors:
        for error in errors:
            print(f"execution attestation: FAIL: {error}", file=sys.stderr)
        return 1

    expected, _ = expected_execution_jobs(plan, args.workflow)
    print(
        "execution attestation: PASS: "
        + (", ".join(expected) if expected else "no predecessor jobs selected")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
