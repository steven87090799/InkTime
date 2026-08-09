from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATHS = (
    REPOSITORY_ROOT / ".github/workflows/ci.yml",
    REPOSITORY_ROOT / ".github/workflows/container-security.yml",
)
FULL_VALIDATION_EXPRESSION = (
    "github.event_name != 'pull_request' || github.event.action != 'edited' || "
    "github.event.changes.base != null"
)
METADATA_LANE_EXPRESSION = (
    "github.event_name == 'pull_request' && github.event.action == 'edited' && "
    "github.event.changes.base == null && 'metadata-only' || 'validation'"
)
CI_HEAVY_JOBS = (
    "python-quality",
    "python-compatibility",
    "dependency-audit",
    "migration-contract",
    "secret-scan",
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


def _job_block(workflow: str, job_id: str) -> str:
    marker = f"  {job_id}:\n"
    lines = workflow.splitlines(keepends=True)
    start = lines.index(marker)
    end = next(
        (
            index
            for index, line in enumerate(lines[start + 1 :], start + 1)
            if line.startswith("  ")
            and not line.startswith("    ")
            and line.rstrip().endswith(":")
        ),
        len(lines),
    )
    return "".join(lines[start:end])


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
        ("pull_request", "ready_for_review"),
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
def test_workflows_isolate_metadata_concurrency_before_jobs_start(workflow_path):
    workflow = workflow_path.read_text(encoding="utf-8")

    assert (
        "types: [opened, synchronize, reopened, ready_for_review, labeled, "
        "unlabeled, edited]"
    ) in workflow
    assert METADATA_LANE_EXPRESSION in workflow
    assert "cancel-in-progress: true" in workflow
    assert "pull_request_target:" not in workflow


@pytest.mark.parametrize("workflow_path", WORKFLOW_PATHS)
def test_metadata_edits_skip_checkout_and_changed_path_routing(workflow_path):
    workflow = workflow_path.read_text(encoding="utf-8")
    changes = _job_block(workflow, "changes")

    assert f"FULL_VALIDATION: ${{{{ {FULL_VALIDATION_EXPRESSION} }}}}" in changes
    assert "full_validation: ${{ steps.event.outputs.full_validation }}" in changes
    assert changes.count("if: ${{ steps.event.outputs.full_validation == 'true' }}") == 2


@pytest.mark.parametrize(
    "workflow_name,heavy_jobs",
    [
        ("ci.yml", CI_HEAVY_JOBS),
        ("container-security.yml", CONTAINER_HEAVY_JOBS),
    ],
)
def test_all_heavy_jobs_require_full_validation(workflow_name, heavy_jobs):
    workflow = (
        REPOSITORY_ROOT / ".github/workflows" / workflow_name
    ).read_text(encoding="utf-8")

    for job_id in heavy_jobs:
        job = _job_block(workflow, job_id)
        assert "needs: changes" in job, job_id
        assert "needs.changes.outputs.full_validation == 'true'" in job, job_id


def test_required_gate_names_and_metadata_skip_handling_are_preserved():
    ci_workflow = WORKFLOW_PATHS[0].read_text(encoding="utf-8")
    container_workflow = WORKFLOW_PATHS[1].read_text(encoding="utf-8")
    repository_gate = _job_block(ci_workflow, "repository-gate")
    container_gate = _job_block(container_workflow, "container-security-gate")

    assert "name: Repository gate" in repository_gate
    assert "name: Container security gate" in container_gate
    for gate in (repository_gate, container_gate):
        assert "if: ${{ always() }}" in gate
        assert "contains(needs.*.result, 'failure')" in gate
        assert "contains(needs.*.result, 'cancelled')" in gate
