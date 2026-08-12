from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci.run_selected_suites import RUNNER_SUITE_TEST_PATHS


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATHS = (
    REPOSITORY_ROOT / ".github/workflows/ci.yml",
    REPOSITORY_ROOT / ".github/workflows/container-security.yml",
)
EXPECTED_PULL_REQUEST_TYPES = (
    "opened",
    "synchronize",
    "reopened",
    "labeled",
    "unlabeled",
    "edited",
)
FULL_VALIDATION_EXPRESSION = (
    "${{ github.event_name != 'pull_request' || github.event.action != 'edited' || "
    "github.event.changes.base != null }}"
)
METADATA_LANE_EXPRESSION = (
    "github.event_name == 'pull_request' && github.event.action == 'edited' && "
    "github.event.changes.base == null && 'metadata-only' || 'validation'"
)
FULL_VALIDATION_GUARD = "needs.changes.outputs.full_validation == 'true'"
CI_HEAVY_JOBS = (
    "source-head-contract",
    "python-quality",
    "python-compatibility",
    "dependency-audit",
    "migration-contract",
    "secret-scan",
    "actionlint",
    "selected-owner-suites",
    "compose-lan-production-persistence",
    "compose-production-tls-smoke",
    "bounded-runtime-soak",
    "playwright",
    "firmware-host-contract",
    "esp32-compile",
)
CONTAINER_HEAVY_JOBS = (
    "container-security",
    "benchmark-contract",
)


def _load_workflow(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _requires_full_validation(
    event_name: str,
    action: str = "",
    changes: dict[str, object] | None = None,
) -> bool:
    return not (
        event_name == "pull_request"
        and action == "edited"
        and "base" not in (changes or {})
    )


def _concurrency_lane(
    event_name: str,
    action: str = "",
    changes: dict[str, object] | None = None,
) -> str:
    if _requires_full_validation(event_name, action, changes):
        return "validation"
    return "metadata-only"


def _gate_name(
    full_validation: bool,
    validation_name: str,
    metadata_name: str,
) -> str:
    return validation_name if full_validation else metadata_name


def _step_by_id(job: dict[str, Any], step_id: str) -> dict[str, Any]:
    return next(step for step in job["steps"] if step.get("id") == step_id)


def _step_by_name(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in job["steps"] if step.get("name") == name)


@pytest.mark.parametrize("changed_field", ["body", "title"])
def test_metadata_only_edits_do_not_require_full_validation(changed_field):
    changes = {changed_field: {"from": "old value"}}

    assert _requires_full_validation("pull_request", "edited", changes) is False
    assert _concurrency_lane("pull_request", "edited", changes) == "metadata-only"


def test_base_retarget_requires_full_validation():
    changes = {"base": {"ref": {"from": "release"}}}

    assert _requires_full_validation("pull_request", "edited", changes) is True
    assert _concurrency_lane("pull_request", "edited", changes) == "validation"


@pytest.mark.parametrize(
    "event_name,action",
    [
        ("pull_request", "opened"),
        ("pull_request", "synchronize"),
        ("pull_request", "reopened"),
        ("pull_request", "labeled"),
        ("pull_request", "unlabeled"),
        ("push", ""),
        ("workflow_dispatch", ""),
    ],
)
def test_code_and_routing_events_require_full_validation(event_name, action):
    assert _requires_full_validation(event_name, action) is True
    assert _concurrency_lane(event_name, action) == "validation"


@pytest.mark.parametrize("workflow_path", WORKFLOW_PATHS)
def test_workflows_parse_and_isolate_metadata_concurrency(workflow_path):
    workflow = _load_workflow(workflow_path)
    pull_request = workflow["on"]["pull_request"]
    concurrency = workflow["concurrency"]

    assert pull_request["branches"] == ["main"]
    assert pull_request["types"] == list(EXPECTED_PULL_REQUEST_TYPES)
    assert "ready_for_review" not in pull_request["types"]
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert "workflow_dispatch" in workflow["on"]
    assert METADATA_LANE_EXPRESSION in concurrency["group"]
    assert concurrency["cancel-in-progress"] is True
    assert "pull_request_target" not in workflow["on"]


@pytest.mark.parametrize("workflow_path", WORKFLOW_PATHS)
def test_metadata_edits_skip_checkout_routing_and_provenance(workflow_path):
    workflow = _load_workflow(workflow_path)
    changes = workflow["jobs"]["changes"]
    event = _step_by_id(changes, "event")
    route = _step_by_id(changes, "route")
    checkout = next(step for step in changes["steps"] if "uses" in step)
    upload = _step_by_name(changes, "Upload provenance metadata")

    assert changes["outputs"]["full_validation"] == (
        "${{ steps.event.outputs.full_validation }}"
    )
    assert event["env"]["FULL_VALIDATION"] == FULL_VALIDATION_EXPRESSION
    assert checkout["if"] == "${{ steps.event.outputs.full_validation == 'true' }}"
    assert route["if"] == "${{ steps.event.outputs.full_validation == 'true' }}"
    assert upload["if"] == "${{ steps.event.outputs.full_validation == 'true' }}"


@pytest.mark.parametrize(
    "workflow_name,heavy_jobs",
    [
        ("ci.yml", CI_HEAVY_JOBS),
        ("container-security.yml", CONTAINER_HEAVY_JOBS),
    ],
)
def test_all_heavy_jobs_require_full_validation(workflow_name, heavy_jobs):
    workflow = _load_workflow(REPOSITORY_ROOT / ".github/workflows" / workflow_name)

    for job_id in heavy_jobs:
        job = workflow["jobs"][job_id]
        assert job["needs"] == "changes", job_id
        assert FULL_VALIDATION_GUARD in job["if"], job_id


@pytest.mark.parametrize(
    "workflow_path,gate_id,validation_name,metadata_name,attestation_workflow",
    [
        (
            WORKFLOW_PATHS[0],
            "repository-gate",
            "Repository gate",
            "Metadata event gate",
            "repository",
        ),
        (
            WORKFLOW_PATHS[1],
            "container-security-gate",
            "Container security gate",
            "Container metadata event gate",
            "container",
        ),
    ],
)
def test_metadata_uses_distinct_gate_identity(
    workflow_path,
    gate_id,
    validation_name,
    metadata_name,
    attestation_workflow,
):
    workflow = _load_workflow(workflow_path)
    gate = workflow["jobs"][gate_id]
    failure = _step_by_name(gate, "Fail when event classification failed")
    checkout = next(step for step in gate["steps"] if "uses" in step)
    attestation = _step_by_name(gate, "Verify planner-selected execution attestation")
    metadata = _step_by_name(gate, "Accept metadata-only pull request edit")

    expected_name = (
        "${{ needs.changes.outputs.full_validation == 'true' && "
        + f"'{validation_name}' || '{metadata_name}'"
        + " }}"
    )
    assert gate["name"] == expected_name
    assert _gate_name(True, validation_name, metadata_name) == validation_name
    assert _gate_name(False, validation_name, metadata_name) == metadata_name
    assert gate["if"] == "${{ always() }}"
    assert failure["if"] == "${{ needs.changes.result != 'success' }}"
    assert checkout["if"] == "${{ needs.changes.outputs.full_validation == 'true' }}"
    assert attestation["if"] == "${{ needs.changes.outputs.full_validation == 'true' }}"
    assert f"--workflow {attestation_workflow}" in attestation["run"]
    assert metadata["if"] == (
        "${{ needs.changes.outputs.full_validation != 'true' && "
        "needs.changes.result == 'success' }}"
    )


def test_main_canonical_planner_and_provenance_contracts_are_preserved():
    ci = _load_workflow(WORKFLOW_PATHS[0])
    container = _load_workflow(WORKFLOW_PATHS[1])

    for workflow in (ci, container):
        changes = workflow["jobs"]["changes"]
        route = _step_by_id(changes, "route")
        assert changes["name"] == "Classify changed paths and validation provenance"
        assert route["name"] == (
            "Emit canonical source-owned plan and tested checkout provenance"
        )
        assert "scripts/ci/canonical_plan.py" in route["run"]
        assert "scripts/ci/provenance.py" in route["run"]
        assert "plan_json" in changes["outputs"]
        assert "tested_ref_kind" in changes["outputs"]

    workflow_contract = "tests/unit/test_ci_workflow_events.py"
    assert workflow_contract in RUNNER_SUITE_TEST_PATHS["ci_planner_contracts"]
    assert workflow_contract in RUNNER_SUITE_TEST_PATHS["ci_routing_contracts"]


@pytest.mark.parametrize("workflow_path", WORKFLOW_PATHS)
def test_base_provenance_is_event_specific_and_fail_closed(workflow_path):
    workflow = _load_workflow(workflow_path)
    route = _step_by_id(workflow["jobs"]["changes"], "route")
    run = route["run"]

    assert "github.event.pull_request.base.sha" in route["env"]["BASE_SHA"]
    assert "github.event.before" in route["env"]["BASE_SHA"]
    assert "BASE_SHA_INPUT" in route["env"]
    assert "pull_request|push)" in run
    assert "workflow_dispatch)" in run
    assert "refs/remotes/origin/main" in run
    assert "git merge-base --is-ancestor" in run
    assert "git cat-file -e" in run
    assert "git rev-parse HEAD^" not in run
    assert "origin/${base_ref}" not in run


def test_ci_codeowners_documents_ownership_without_approval_gate():
    codeowners = (REPOSITORY_ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8")

    assert "/.github/workflows/ @steven87090799" in codeowners
    assert "/scripts/ci/ @steven87090799" in codeowners
    assert "/.github/CODEOWNERS @steven87090799" in codeowners
    assert "require_code_owner_review" not in codeowners


def test_source_head_artifact_serializes_named_jq_arguments():
    ci = _load_workflow(WORKFLOW_PATHS[0])
    source_head = ci["jobs"]["source-head-contract"]
    verify = _step_by_name(source_head, "Verify declared PR source HEAD provenance")

    assert "jq -n" in verify["run"]
    assert "'$ARGS.named'" in verify["run"]
