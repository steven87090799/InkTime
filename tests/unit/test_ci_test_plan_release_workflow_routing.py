from __future__ import annotations

from scripts.ci.canonical_plan import build_canonical_plan
from scripts.ci.test_plan import FULL_MODE, IMPACT_MODE


def _pr_context() -> dict[str, object]:
    return {
        "event_name": "pull_request",
        "ref": "refs/pull/64/merge",
        "draft": True,
        "labels": [],
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }


def test_release_workflow_only_change_skips_runtime_container_and_nas_gates():
    path = ".github/workflows/publish-container.yml"
    plan = build_canonical_plan([path], _pr_context())

    assert plan["ci_mode"] == IMPACT_MODE
    assert plan["changed_paths"] == [path]
    assert plan["changed_domains"] == ["ci_config"]
    assert plan["production_domains"] == []
    assert {"ci_planner_contracts", "ci_routing_contracts"} <= set(
        plan["selected_test_suites"]
    )
    assert {"secret_scan", "actionlint"} <= set(plan["selected_gates"])
    assert {
        "nas_update_e2e",
        "container_security",
        "docker_lan_persistence",
    }.isdisjoint(plan["selected_gates"])
    assert {
        "docker_runtime_owner",
        "container_configuration_owner",
        "persistence_owner",
        "backup_restore_owner",
    }.isdisjoint(plan["selected_test_suites"])


def test_release_workflow_alias_is_disabled_for_mixed_runtime_changes():
    plan = build_canonical_plan(
        [
            ".github/workflows/publish-container.yml",
            "scripts/update_nas.sh",
        ],
        _pr_context(),
    )

    assert {"docker", "persistence", "backup_restore", "ci_config"} <= set(
        plan["changed_domains"]
    )
    assert {"nas_update_e2e", "container_security"} <= set(plan["selected_gates"])


def test_release_workflow_main_push_keeps_full_validation():
    plan = build_canonical_plan(
        [".github/workflows/publish-container.yml"],
        {
            "event_name": "push",
            "ref": "refs/heads/main",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
        },
    )

    assert plan["ci_mode"] == FULL_MODE
    assert {"nas_update_e2e", "container_security", "repository_gate"} <= set(
        plan["selected_gates"]
    )
    assert plan["full_plan_complete"] is True
