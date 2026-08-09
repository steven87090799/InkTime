from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.canonical_plan import build_canonical_plan
from scripts.ci.run_selected_suites import RUNNER_SUITE_TEST_PATHS, selected_test_paths
from scripts.ci.test_plan import FULL_MODE, IMPACT_MODE
from scripts.ci.verify_execution import (
    CONTAINER_WORKFLOW,
    REPOSITORY_WORKFLOW,
    expected_execution_jobs,
)


ROOT = Path(__file__).resolve().parents[2]


def _pr_context(*, draft: bool = True, labels: list[str] | None = None) -> dict[str, object]:
    return {
        "event_name": "pull_request",
        "ref": "refs/pull/64/merge",
        "draft": draft,
        "labels": labels or [],
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }


@pytest.mark.parametrize(
    ("name", "paths", "context", "mode", "required_suites", "required_gates"),
    [
        (
            "docs-only Draft",
            ["docs/CI_POLICY.md"],
            _pr_context(),
            IMPACT_MODE,
            {"ci_planner_contracts", "docs_contract"},
            {"secret_scan"},
        ),
        (
            "ordinary Python Draft",
            ["inktime/app/api/photos.py"],
            _pr_context(),
            IMPACT_MODE,
            {"python_application_owner"},
            {"secret_scan"},
        ),
        (
            "provider Draft",
            ["inktime/app/services/analysis.py"],
            _pr_context(),
            IMPACT_MODE,
            {"provider_analysis_owner"},
            {"benchmark"},
        ),
        (
            "auth/session Draft",
            ["inktime/app/api/session.py"],
            _pr_context(),
            IMPACT_MODE,
            {"auth_security_owner", "web_api_owner", "web_e2e"},
            {"playwright", "tls_smoke"},
        ),
        (
            "runtime Draft",
            ["inktime/app/workers/scheduler.py"],
            _pr_context(),
            IMPACT_MODE,
            {"runtime_scheduler_owner"},
            {"runtime_soak"},
        ),
        (
            "migration Draft",
            ["inktime/app/db/migrations.py"],
            _pr_context(),
            IMPACT_MODE,
            {"persistence_owner", "migration_owner"},
            {"secret_scan"},
        ),
        (
            "Docker Draft",
            ["Dockerfile"],
            _pr_context(),
            IMPACT_MODE,
            {"docker_runtime_owner", "container_configuration_owner"},
            {"container_security"},
        ),
        (
            "firmware-specific Draft",
            ["esp32/ink-display-7C-photo/photopainter_core.h"],
            _pr_context(),
            IMPACT_MODE,
            {"firmware_host_contract_tests"},
            {"firmware_host_contract", "firmware_quick", "firmware_affected"},
        ),
        (
            "unknown production path",
            ["inktime/new_runtime_surface.cfg"],
            _pr_context(),
            FULL_MODE,
            {"python312_unit_security_integration_coverage"},
            {"repository_gate", "container_security_gate", "actionlint"},
        ),
        (
            "ready PR full",
            ["inktime/app/api/photos.py"],
            _pr_context(draft=False),
            FULL_MODE,
            {"python312_unit_security_integration_coverage"},
            {"repository_gate", "container_security_gate", "actionlint"},
        ),
        (
            "full-ci full",
            ["inktime/app/api/photos.py"],
            _pr_context(labels=["full-ci"]),
            FULL_MODE,
            {"python312_unit_security_integration_coverage"},
            {"repository_gate", "container_security_gate", "actionlint"},
        ),
        (
            "main push full",
            ["inktime/app/api/photos.py"],
            {
                "event_name": "push",
                "ref": "refs/heads/main",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            },
            FULL_MODE,
            {"python312_unit_security_integration_coverage"},
            {"repository_gate", "container_security_gate", "actionlint"},
        ),
        (
            "workflow_dispatch full",
            ["inktime/app/api/photos.py"],
            {
                "event_name": "workflow_dispatch",
                "ref": "refs/heads/main",
                "full_suite": True,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            },
            FULL_MODE,
            {"python312_unit_security_integration_coverage"},
            {"repository_gate", "container_security_gate", "actionlint"},
        ),
    ],
)
def test_planner_hardening_matrix(
    name, paths, context, mode, required_suites, required_gates
):
    plan = build_canonical_plan(paths, context)

    assert plan["ci_mode"] == mode, name
    assert required_suites <= set(plan["selected_test_suites"]), name
    assert required_gates <= set(plan["selected_gates"]), name
    assert plan["owner_suite_gaps"] == {}, name
    assert plan["suite_execution_gaps"] == [], name
    assert plan["no_heavy_impact_duplicates"] is True, name
    assert plan["full_plan_complete"] is (mode == FULL_MODE), name
    assert plan["source_head_sha"] == "b" * 40, name
    assert plan["event_name"] == context["event_name"], name
    assert plan["requires_source_head_contract"] is (
        context["event_name"] == "pull_request"
    ), name

    for workflow in (REPOSITORY_WORKFLOW, CONTAINER_WORKFLOW):
        expected, errors = expected_execution_jobs(plan, workflow)
        assert errors == [], (name, workflow, errors)
        assert "changes" in expected, (name, workflow, expected)


@pytest.mark.parametrize(
    ("path", "expected_domains", "expected_suites"),
    [
        (
            "inktime/app/domain/jobs/failure_policy.py",
            {"python", "runtime"},
            {"python_application_owner", "runtime_scheduler_owner"},
        ),
        (
            "inktime/app/api/resilience.py",
            {"python", "runtime", "queue_resilience"},
            {
                "python_application_owner",
                "runtime_scheduler_owner",
                "queue_resilience_owner",
            },
        ),
        (
            "inktime/app/repositories/offline_schedules.py",
            {"python", "persistence", "runtime", "queue_resilience"},
            {
                "python_application_owner",
                "persistence_owner",
                "runtime_scheduler_owner",
                "queue_resilience_owner",
            },
        ),
        (
            "inktime/app/services/release_coordinator.py",
            {"python", "runtime", "render_release"},
            {
                "python_application_owner",
                "runtime_scheduler_owner",
                "render_release_owner",
            },
        ),
    ],
)
def test_production_cross_layer_paths_route_required_owners(
    path, expected_domains, expected_suites
):
    plan = build_test_plan([path], _pr_context())

    assert expected_domains <= set(plan["changed_domains"])
    assert expected_suites <= set(plan["selected_test_suites"])
    assert "runtime_soak" in plan["expensive_gates"]
    assert plan["production_owner_invariant"] is True
    assert plan["suite_execution_gaps"] == []


def test_provider_and_render_owners_include_cross_layer_regressions():
    assert {
        "tests/integration/test_analysis_pipeline.py",
        "tests/integration/test_ai_cache_singleflight.py",
        "tests/integration/test_photo_quality_ai.py",
    } <= set(RUNNER_SUITE_TEST_PATHS["provider_analysis_owner"])
    assert {
        "tests/integration/test_adaptive_frame_renderer.py",
        "tests/integration/test_dual_photo_caption_layout.py",
        "tests/integration/test_render_candidate_contract.py",
    } <= set(RUNNER_SUITE_TEST_PATHS["render_release_owner"])


def test_overlapping_owner_paths_are_deduplicated():
    _suites, paths = selected_test_paths(
        ["device_api_contract_owner", "device_delivery_owner", "queue_resilience_owner"]
    )
    assert len(paths) == len(set(paths))


def test_workflows_attest_selected_execution_and_distinguish_provenance():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    container = (ROOT / ".github/workflows/container-security.yml").read_text(
        encoding="utf-8"
    )

    for workflow in (ci, container):
        assert "SOURCE_HEAD_SHA" in workflow
        assert "BASE_SHA" in workflow
        assert "TESTED_SHA" in workflow
        assert "TESTED_REF_KIND" in workflow
        assert "toJSON(needs)" in workflow
        assert "scripts/ci/verify_execution.py" in workflow
        assert "exact pushed HEAD" not in workflow

    assert "source-head-contract:" in ci
    assert "ref: ${{ github.event.pull_request.head.sha }}" in ci
    assert "merge-ref" in ci
    assert "--workflow repository" in ci
    assert "--workflow container" in container
