from __future__ import annotations

from copy import deepcopy

from scripts.ci.test_plan import build_test_plan
from scripts.ci.verify_execution import (
    CONTAINER_WORKFLOW,
    REPOSITORY_WORKFLOW,
    expected_execution_jobs,
    verify_execution,
)


def _context(*, draft: bool = True) -> dict[str, object]:
    return {
        "event_name": "pull_request",
        "ref": "refs/pull/64/merge",
        "draft": draft,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }


def _needs_for(plan: dict[str, object], workflow: str) -> dict[str, dict[str, str]]:
    jobs, errors = expected_execution_jobs(plan, workflow)
    assert errors == []
    return {job: {"result": "success"} for job in jobs}


def test_selected_success_passes():
    plan = build_test_plan(["inktime/app/workers/scheduler.py"], _context())
    needs = _needs_for(plan, REPOSITORY_WORKFLOW)

    assert verify_execution(plan, needs, REPOSITORY_WORKFLOW) == []


def test_selected_skipped_fails_closed():
    plan = build_test_plan(["inktime/app/workers/scheduler.py"], _context())
    needs = _needs_for(plan, REPOSITORY_WORKFLOW)
    needs["bounded-runtime-soak"]["result"] = "skipped"

    errors = verify_execution(plan, needs, REPOSITORY_WORKFLOW)
    assert any("bounded-runtime-soak" in error and "skipped" in error for error in errors)


def test_selected_failure_fails_closed():
    plan = build_test_plan(["inktime/app/workers/scheduler.py"], _context())
    needs = _needs_for(plan, REPOSITORY_WORKFLOW)
    needs["bounded-runtime-soak"]["result"] = "failure"

    errors = verify_execution(plan, needs, REPOSITORY_WORKFLOW)
    assert any("bounded-runtime-soak" in error and "failure" in error for error in errors)


def test_unselected_skipped_job_is_accepted_in_impact_mode():
    plan = build_test_plan(["README.md"], _context())
    needs = _needs_for(plan, REPOSITORY_WORKFLOW)
    needs["playwright"] = {"result": "skipped"}
    needs["esp32-compile"] = {"result": "skipped"}

    assert verify_execution(plan, needs, REPOSITORY_WORKFLOW) == []


def test_full_mode_missing_required_job_fails_closed():
    plan = build_test_plan(["README.md"], _context(draft=False))
    needs = _needs_for(plan, REPOSITORY_WORKFLOW)
    missing_job = "python-compatibility"
    assert missing_job in needs
    del needs[missing_job]

    errors = verify_execution(plan, needs, REPOSITORY_WORKFLOW)
    assert any(missing_job in error and "missing" in error for error in errors)


def test_unknown_selected_execution_fails_closed():
    plan = build_test_plan(["README.md"], _context())
    tampered = deepcopy(plan)
    tampered["selected_test_suites"] = [*plan["selected_test_suites"], "unknown_selected_suite"]
    needs = _needs_for(plan, REPOSITORY_WORKFLOW)

    errors = verify_execution(tampered, needs, REPOSITORY_WORKFLOW)
    assert any("unknown_selected_suite" in error for error in errors)


def test_impact_container_workflow_accepts_unrelated_skipped_jobs():
    plan = build_test_plan(["Dockerfile"], _context())
    needs = _needs_for(plan, CONTAINER_WORKFLOW)
    needs["benchmark-contract"] = {"result": "skipped"}

    assert verify_execution(plan, needs, CONTAINER_WORKFLOW) == []


def test_full_mode_accepts_only_intentionally_non_applicable_impact_runner_skip():
    plan = build_test_plan(["README.md"], _context(draft=False))
    needs = _needs_for(plan, REPOSITORY_WORKFLOW)
    needs["selected_owner_suites"] = {"result": "skipped"}

    assert "selected_owner_suites" not in expected_execution_jobs(
        plan, REPOSITORY_WORKFLOW
    )[0]
    assert verify_execution(plan, needs, REPOSITORY_WORKFLOW) == []
