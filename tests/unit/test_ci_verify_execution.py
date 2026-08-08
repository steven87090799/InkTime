import pytest

from scripts.ci.test_plan import WORKFLOW_EXECUTION_JOB_IDS, build_test_plan
from scripts.ci.verify_execution import (
    AGGREGATE_JOB_BY_WORKFLOW,
    expected_execution_jobs,
    verify_execution,
)


def _context(*, full: bool = False) -> dict[str, object]:
    return {
        "event_name": "pull_request",
        "ref": "refs/pull/64/merge",
        "draft": not full,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "source_head_sha": "b" * 40,
        "tested_sha": "c" * 40,
        "tested_ref_kind": "merge-ref",
    }


def _plan(*, full: bool = False):
    plan = build_test_plan(
        ["README.md" if full else "inktime/app/providers/openrouter.py"],
        _context(full=full),
    )
    plan["requires_source_head_contract"] = True
    return plan


def _needs(plan, workflow: str) -> dict[str, dict[str, str]]:
    workflow_identity = "repository" if workflow == "ci" else workflow
    expected, errors = expected_execution_jobs(plan, workflow_identity)
    assert errors == []
    needs = {
        job: {"result": "skipped"}
        for job in WORKFLOW_EXECUTION_JOB_IDS[workflow]
        if job != AGGREGATE_JOB_BY_WORKFLOW[workflow_identity]
    }
    for job in expected:
        needs[job] = {"result": "success"}
    return needs


def test_selected_success_passes():
    plan = _plan()
    assert verify_execution(plan, _needs(plan, "ci"), "ci") == []


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled"])
def test_selected_non_success_fails_closed(result):
    plan = _plan()
    needs = _needs(plan, "ci")
    needs["selected-owner-suites"] = {"result": result}

    errors = verify_execution(plan, needs, "ci")

    assert any(
        "execution_id=suite:ci_planner_contracts" in error
        and "job_name=selected-owner-suites" in error
        and f"actual_result={result}" in error
        for error in errors
    )


def test_selected_missing_fails_closed():
    plan = _plan()
    needs = _needs(plan, "ci")
    del needs["selected-owner-suites"]

    errors = verify_execution(plan, needs, "ci")

    assert any(
        "execution_id=suite:ci_planner_contracts" in error
        and "job_name=selected-owner-suites" in error
        and "actual_result=missing" in error
        for error in errors
    )


def test_unknown_execution_fails_closed():
    plan = _plan()
    needs = _needs(plan, "ci")
    needs["workflow-drift-job"] = {"result": "success"}

    errors = verify_execution(plan, needs, "ci")

    assert any(
        "execution_id=unknown:workflow-drift-job" in error
        and "job_name=workflow-drift-job" in error
        and "actual_result=unknown" in error
        for error in errors
    )


def test_unselected_skipped_is_allowed_in_impact_mode():
    plan = _plan()
    needs = _needs(plan, "ci")

    assert needs["python-compatibility"]["result"] == "skipped"
    assert verify_execution(plan, needs, "ci") == []


def test_unrelated_container_jobs_skipped_are_allowed_in_impact_mode():
    plan = _plan()
    needs = _needs(plan, "container")

    assert needs["container-security"]["result"] == "skipped"
    assert needs["benchmark-contract"]["result"] == "skipped"
    assert verify_execution(plan, needs, "container") == []


@pytest.mark.parametrize("workflow", ["ci", "container"])
def test_full_mode_all_required_jobs_success(workflow):
    plan = _plan(full=True)

    assert verify_execution(plan, _needs(plan, workflow), workflow) == []


@pytest.mark.parametrize("workflow", ["ci", "container"])
def test_full_mode_required_job_skipped_fails(workflow):
    plan = _plan(full=True)
    needs = _needs(plan, workflow)
    required_job = expected_execution_jobs(plan, workflow)[0][0]
    needs[required_job] = {"result": "skipped"}

    errors = verify_execution(plan, needs, workflow)

    assert any(
        f"job_name={required_job}" in error
        and "expected_result=success" in error
        and "actual_result=skipped" in error
        for error in errors
    )


def test_selected_owner_suites_is_intentionally_skipped_in_full_mode():
    plan = _plan(full=True)
    needs = _needs(plan, "ci")
    needs["selected-owner-suites"] = {"result": "skipped"}

    assert "selected-owner-suites" not in expected_execution_jobs(plan, "ci")[0]
    assert verify_execution(plan, needs, "ci") == []
