import json

from scripts.ci.test_plan import (
    DOMAIN_OWNER_SUITES,
    FULL_EXPENSIVE_GATES,
    FULL_EXECUTION_OWNERS,
    FULL_FIRMWARE_PROFILES,
    FULL_MODE,
    FULL_PLAN_SUITES,
    FULL_SUITE_EXECUTION_OWNERS,
    FULL_TEST_SUITES,
    IMPACT_MODE,
    PRODUCTION_DOMAINS,
    build_test_plan,
    resolve_ci_mode,
)


def draft_context(**overrides):
    context = {
        "event_name": "pull_request",
        "draft": True,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "source_head_sha": "b" * 40,
        "tested_sha": "c" * 40,
        "tested_ref_kind": "merge-ref",
        "ref": "refs/pull/64/merge",
    }
    context.update(overrides)
    return context


def test_docs_only_is_bounded_and_has_a_non_empty_tier_zero_plan():
    plan = build_test_plan(["README.md", "docs/README.md"], draft_context())

    assert plan["ci_mode"] == IMPACT_MODE
    assert plan["docs_only"] is True
    assert plan["production_domains"] == []
    assert plan["selected_test_suites"]
    assert "ci_planner_contracts" in plan["selected_owner_suites"]
    assert "docs_contract" in plan["selected_test_suites"]
    assert plan["suite_execution_gaps"] == []
    assert plan["expensive_gates"] == []
    assert plan["selected_gates"] == ["secret_scan"]
    assert plan["skipped_gates"]


def test_ordinary_python_change_routes_its_owner_without_python_full_suite():
    plan = build_test_plan(["inktime/app/api/photos.py"], draft_context())

    assert "python" in plan["changed_domains"]
    assert "python_application_owner" in plan["selected_test_suites"]
    assert "python_application_owner" in plan["selected_owner_suites"]
    assert plan["production_owner_invariant"] is True
    assert plan["ci_mode"] == IMPACT_MODE
    assert "secret_scan" in plan["selected_gates"]
    assert "python312_full" not in plan["expensive_gates"]


def test_runtime_scheduler_routes_runtime_owner_and_bounded_soak():
    plan = build_test_plan(["inktime/app/workers/scheduler.py"], draft_context())

    assert {"python", "runtime"} <= set(plan["changed_domains"])
    assert "runtime_scheduler_owner" in plan["selected_test_suites"]
    assert "runtime_soak" in plan["expensive_gates"]
    assert plan["ci_mode"] == IMPACT_MODE


def test_persistence_migration_and_backup_are_distinct_owner_domains():
    plan = build_test_plan(
        [
            "inktime/app/db/connection.py",
            "inktime/app/db/migrations.py",
            "scripts/restore_backup.py",
        ],
        draft_context(),
    )

    assert {"persistence", "migration", "backup_restore"} <= set(plan["changed_domains"])
    assert {
        "persistence_owner",
        "migration_owner",
        "backup_restore_owner",
    } <= set(plan["selected_test_suites"])
    assert "docker_lan_persistence" in plan["expensive_gates"]


def test_auth_session_and_csrf_fan_out_to_web_and_tls_boundaries():
    plan = build_test_plan(["inktime/app/api/session.py"], draft_context())

    assert {"python", "web_ui", "auth_security", "tls_security"} <= set(plan["changed_domains"])
    assert {"auth_security_owner", "web_api_owner", "tls_configuration_owner"} <= set(
        plan["selected_test_suites"]
    )
    assert {"playwright", "tls_smoke"} <= set(plan["expensive_gates"])


def test_device_and_firmware_paths_union_without_unrelated_python_full():
    plan = build_test_plan(
        [
            "inktime/app/api/devices.py",
            "esp32/ink-display-7C-photo/queue_client_core.h",
        ],
        draft_context(),
    )

    assert {"device", "firmware"} <= set(plan["changed_domains"])
    assert "device_api_contract_owner" in plan["selected_test_suites"]
    assert "firmware_host_contract_tests" in plan["selected_test_suites"]
    assert "firmware_quick" in plan["expensive_gates"]
    assert "python312_full" not in plan["expensive_gates"]


def test_device_api_is_also_a_server_firmware_host_contract():
    plan = build_test_plan(["inktime/app/api/devices.py"], draft_context())

    assert {"device", "firmware"} <= set(plan["changed_domains"])
    assert "firmware_host_contract_tests" in plan["selected_test_suites"]
    assert "firmware_host_contract" in plan["expensive_gates"]
    assert "firmware_quick" in plan["expensive_gates"]
    assert "firmware_affected" in plan["expensive_gates"]
    assert plan["firmware_execution_profiles"] == {
        "quick": ["photopainter_release"],
        "affected": [
            profile
            for profile in FULL_FIRMWARE_PROFILES
            if profile != "photopainter_release"
        ],
        "full_matrix": [],
    }
    assert plan["no_heavy_impact_duplicates"] is True


def test_queue_and_persistence_paths_preserve_both_owners():
    plan = build_test_plan(
        [
            "inktime/app/services/device_queue_manifests.py",
            "inktime/app/repositories/resilience.py",
        ],
        draft_context(),
    )

    assert {"queue_resilience", "persistence", "runtime"} <= set(plan["changed_domains"])
    assert {
        "queue_resilience_owner",
        "persistence_owner",
        "runtime_scheduler_owner",
    } <= set(
        plan["selected_test_suites"]
    )


def test_provider_transport_does_not_start_unrelated_benchmark():
    plan = build_test_plan(["inktime/app/providers/openrouter.py"], draft_context())

    assert "provider_ai" in plan["changed_domains"]
    assert "provider_analysis_owner" in plan["selected_test_suites"]
    assert "benchmark" not in plan["expensive_gates"]


def test_offline_benchmark_direct_dependencies_start_benchmark_contract():
    direct_dependencies = (
        "inktime/app/providers/base.py",
        "inktime/app/providers/config.py",
        "inktime/app/providers/openai_compatible.py",
        "inktime/app/domain/analysis/__init__.py",
        "inktime/app/domain/analysis/plan.py",
        "inktime/app/domain/analysis/schema.py",
        "inktime/app/domain/analysis/scoring.py",
        "inktime/app/services/analysis.py",
    )

    for path in direct_dependencies:
        plan = build_test_plan([path], draft_context())
        assert "provider_analysis_owner" in plan["selected_test_suites"], path
        assert "benchmark_contract" in plan["selected_test_suites"], path
        assert "benchmark" in plan["expensive_gates"], path


def test_unrelated_provider_implementation_does_not_start_benchmark():
    plan = build_test_plan(["inktime/app/providers/openai_provider.py"], draft_context())

    assert "provider_analysis_owner" in plan["selected_test_suites"]
    assert "benchmark_contract" not in plan["selected_test_suites"]
    assert "benchmark" not in plan["expensive_gates"]


def test_production_preflight_routes_tls_and_lan_persistence():
    plan = build_test_plan(["scripts/production_preflight.py"], draft_context())

    assert plan["changed_domains"] == ["tls_security"]
    assert "tls_configuration_owner" in plan["selected_test_suites"]
    assert {"tls_smoke", "docker_lan_persistence"} <= set(plan["expensive_gates"])
    assert "runtime_soak" not in plan["expensive_gates"]
    assert "container_security" not in plan["expensive_gates"]


def test_production_tls_smoke_remains_tls_only():
    plan = build_test_plan(["scripts/production_tls_smoke.py"], draft_context())

    assert plan["changed_domains"] == ["tls_security"]
    assert "tls_configuration_owner" in plan["selected_test_suites"]
    assert plan["expensive_gates"] == ["tls_smoke"]


def test_pyproject_routes_python310_compatibility_without_container_security():
    plan = build_test_plan(["pyproject.toml"], draft_context())

    assert plan["changed_domains"] == ["dev_dependencies"]
    assert {"dependency_policy", "ruff", "mypy"} <= set(plan["selected_test_suites"])
    assert "python310_compatibility" in plan["expensive_gates"]
    assert "container_security" not in plan["expensive_gates"]


def test_test_only_backup_restore_uses_focused_owner_runner():
    plan = build_test_plan(["tests/unit/test_backups.py"], draft_context())

    assert plan["test_only"] is True
    assert "backup_restore_owner" in plan["selected_test_suites"]
    assert "backup_restore_owner" in plan["selected_owner_suites"]
    assert "docker_lan_persistence" not in plan["expensive_gates"]
    assert plan["suite_execution_gaps"] == []


def test_platform_routes_auth_security_and_tls_smoke():
    plan = build_test_plan(["inktime/app/platform.py"], draft_context())

    assert {"python", "auth_security", "tls_security"} <= set(plan["changed_domains"])
    assert {"python_application_owner", "auth_security_owner", "tls_configuration_owner"} <= set(
        plan["selected_test_suites"]
    )
    assert "tls_smoke" in plan["expensive_gates"]
    assert "playwright" not in plan["expensive_gates"]


def test_benchmark_contract_is_bounded_to_benchmark_changes():
    plan = build_test_plan(["scripts/benchmark_models.py"], draft_context())

    assert "benchmark" in plan["changed_domains"]
    assert "benchmark" in plan["expensive_gates"]
    assert plan["ci_mode"] == IMPACT_MODE


def test_production_dependency_routes_python_and_container_security():
    plan = build_test_plan(["requirements.txt"], draft_context())

    assert "dependencies" in plan["changed_domains"]
    assert "python_dependency_owner" in plan["selected_test_suites"]
    assert {"dependency_audit", "container_security"} <= set(plan["expensive_gates"])
    assert plan["ci_mode"] == IMPACT_MODE


def test_dev_dependency_does_not_start_production_docker_gate():
    plan = build_test_plan(["requirements-dev.txt"], draft_context())

    assert "dev_dependencies" in plan["changed_domains"]
    assert "dependency_policy" in plan["selected_test_suites"]
    assert "docker_lan_persistence" not in plan["expensive_gates"]
    assert "container_security" not in plan["expensive_gates"]
    assert plan["ci_mode"] == IMPACT_MODE


def test_e2e_tooling_routes_playwright_without_full_mode():
    plan = build_test_plan(["requirements-e2e.txt"], draft_context())

    assert "e2e_tooling" in plan["changed_domains"]
    assert "playwright" in plan["expensive_gates"]
    assert plan["ci_mode"] == IMPACT_MODE


def test_ci_workflow_change_is_known_ci_config_not_full_in_draft():
    plan = build_test_plan([".github/workflows/ci.yml"], draft_context())

    assert plan["changed_domains"] == ["ci_config"]
    assert "ci_routing_contracts" in plan["selected_test_suites"]
    assert "actionlint" in plan["expensive_gates"]
    assert plan["ci_mode"] == IMPACT_MODE
    assert plan["unknown_paths"] == []


def test_tls_smoke_workflow_surface_is_not_downgraded_to_plain_ci_config():
    plan = build_test_plan([".github/tls-smoke/docker-compose.tls.yml"], draft_context())

    assert {"ci_config", "docker", "tls_security"} <= set(plan["changed_domains"])
    assert {"tls_smoke", "container_security"} <= set(plan["expensive_gates"])
    assert "docker_lan_persistence" not in plan["expensive_gates"]
    assert plan["ci_mode"] == IMPACT_MODE


def test_test_only_changes_route_their_family_and_not_production_owners():
    plan = build_test_plan(["tests/unit/test_dates.py"], draft_context())

    assert plan["test_only"] is True
    assert plan["production_domains"] == []
    assert "unit_owner" in plan["selected_test_suites"]
    assert plan["ci_mode"] == IMPACT_MODE


def test_test_only_runtime_file_does_not_become_a_production_change():
    plan = build_test_plan(["tests/integration/test_scheduler.py"], draft_context())

    assert plan["test_only"] is True
    assert plan["production_domains"] == []
    assert plan["owner_suite_gaps"] == {}
    assert "runtime_scheduler_owner" in plan["selected_test_suites"]


def test_overlapping_test_path_fans_out_to_all_relevant_test_owners():
    plan = build_test_plan(
        ["tests/unit/test_esp32_offline_schedule_device_session.py"], draft_context()
    )

    assert plan["test_only"] is True
    assert plan["production_domains"] == []
    assert {"firmware", "runtime", "queue_resilience", "device", "auth_security"} <= set(
        plan["changed_domains"]
    )
    assert {
        "firmware_host_contract_tests",
        "runtime_scheduler_owner",
        "queue_resilience_owner",
        "device_delivery_owner",
        "auth_security_owner",
        "web_e2e",
    } <= set(plan["selected_test_suites"])
    assert {"firmware_host_contract", "runtime_soak", "playwright"} <= set(
        plan["expensive_gates"]
    )


def test_firmware_facing_unit_and_device_tests_have_deterministic_contract_owners():
    esp32 = build_test_plan(
        ["tests/unit/test_esp32_config_store_contract.py"], draft_context()
    )
    device = build_test_plan(["tests/security/test_device_pairing.py"], draft_context())

    assert "firmware" in esp32["changed_domains"]
    assert "firmware_host_contract_tests" in esp32["selected_test_suites"]
    assert "firmware_host_contract" in esp32["expensive_gates"]
    assert {"device", "firmware", "auth_security"} <= set(device["changed_domains"])
    assert {
        "device_delivery_owner",
        "firmware_host_contract_tests",
        "auth_security_owner",
    } <= set(device["selected_test_suites"])


def test_test_session_contract_routes_browser_execution():
    plan = build_test_plan(["tests/security/test_session.py"], draft_context())

    assert plan["test_only"] is True
    assert "web_e2e" in plan["selected_test_suites"]
    assert "playwright" in plan["expensive_gates"]


def test_test_migration_and_restore_paths_fan_out_to_persistence_and_specialized_owners():
    migration = build_test_plan(["tests/unit/test_migrations.py"], draft_context())
    restore = build_test_plan(["tests/unit/test_restore_backup.py"], draft_context())
    soak = build_test_plan(["tests/unit/test_runtime_soak.py"], draft_context())

    assert {"persistence", "migration"} <= set(migration["changed_domains"])
    assert {"persistence_owner", "migration_owner"} <= set(
        migration["selected_test_suites"]
    )
    assert {"persistence", "backup_restore"} <= set(restore["changed_domains"])
    assert {"persistence_owner", "backup_restore_owner"} <= set(
        restore["selected_test_suites"]
    )
    assert "runtime" in soak["changed_domains"]
    assert "runtime_soak" in soak["expensive_gates"]


def test_unknown_path_fails_open_to_a_complete_full_plan():
    plan = build_test_plan(["new-production-surface.toml"], draft_context())

    assert plan["ci_mode"] == FULL_MODE
    assert plan["unknown_paths"] == ["new-production-surface.toml"]
    assert "unknown_production_or_config_path" in plan["why_full_suite"]
    assert plan["full_plan_complete"] is True
    assert set(FULL_TEST_SUITES) <= set(plan["selected_test_suites"])
    assert set(FULL_EXPENSIVE_GATES) <= set(plan["expensive_gates"])
    assert set(FULL_PLAN_SUITES) <= set(plan["selected_test_suites"])
    assert "secret_scan" in plan["selected_gates"]


def test_full_mode_replaces_impact_heavy_gates_without_duplicate_impact_jobs():
    plan = build_test_plan(["inktime/app/workers/scheduler.py"], draft_context(draft=False))

    assert plan["ci_mode"] == FULL_MODE
    assert plan["selected_test_suites"] == list(FULL_PLAN_SUITES)
    assert plan["selected_owner_suites"] == []
    assert plan["suite_execution_gaps"] == []
    assert plan["full_suite_execution_gaps"] == []
    assert plan["expensive_gates"] == list(FULL_EXPENSIVE_GATES)
    assert "secret_scan" in plan["selected_gates"]
    assert "mypy" in plan["selected_test_suites"]
    assert "dependency_policy" in plan["selected_test_suites"]
    assert "runtime_scheduler_owner" in plan["selected_test_suites"]
    assert plan["no_heavy_impact_duplicates"] is True
    assert not set(plan["selected_test_suites"]) & set(plan["expensive_gates"])
    assert plan["full_execution_ids"]
    assert plan["impact_execution_ids"] == []


def test_impact_mode_allows_intended_heavy_owner_gate_without_duplicate_flag():
    plan = build_test_plan(["Dockerfile"], draft_context())

    assert "container_security" in plan["expensive_gates"]
    assert "docker_lan_persistence" not in plan["expensive_gates"]
    assert plan["no_heavy_impact_duplicates"] is True


def test_full_mode_semantics_cover_ready_label_main_and_manual_events():
    assert resolve_ci_mode(draft_context(draft=False)) == FULL_MODE
    assert resolve_ci_mode(draft_context(labels=["full-ci"])) == FULL_MODE
    assert (
        resolve_ci_mode(
            {"event_name": "push", "ref": "refs/heads/main", "draft": True}
        )
        == FULL_MODE
    )
    assert (
        resolve_ci_mode(
            {"event_name": "workflow_dispatch", "full_suite": True, "draft": True}
        )
        == FULL_MODE
    )


def test_ready_synchronize_is_full_and_draft_synchronize_is_impact():
    assert resolve_ci_mode(draft_context(action="synchronize")) == IMPACT_MODE
    assert (
        resolve_ci_mode(draft_context(draft=False, action="synchronize"))
        == FULL_MODE
    )


def test_missing_pull_request_draft_state_fails_open():
    assert resolve_ci_mode({"event_name": "pull_request"}) == FULL_MODE


def test_multi_domain_union_is_deterministic_and_preserves_sha_context():
    plan = build_test_plan(
        [
            "./inktime/app/providers/router.py",
            "inktime/app/services/model_benchmark.py",
            "Dockerfile",
        ],
        draft_context(),
    )

    assert {"provider_ai", "benchmark", "docker"} <= set(plan["changed_domains"])
    assert plan["base_sha"] == "a" * 40
    assert plan["head_sha"] == "b" * 40
    assert plan["changed_paths"] == [
        "inktime/app/providers/router.py",
        "inktime/app/services/model_benchmark.py",
        "Dockerfile",
    ]


def test_path_normalisation_deduplicates_and_plan_is_json_safe():
    plan = build_test_plan(
        ["README.md", "./README.md", "README.md"],
        draft_context(base_sha=123, head_sha=456),
    )

    assert plan["changed_paths"] == ["README.md"]
    assert plan["base_sha"] == "123"
    assert plan["head_sha"] == "456"
    json.dumps(plan, sort_keys=True)


def test_firmware_impact_reports_profile_specific_selection_and_shared_surface():
    photopainter = build_test_plan(
        ["esp32/ink-display-7C-photo/photopainter_core.h"], draft_context()
    )
    shared = build_test_plan(
        ["esp32/ink-display-7C-photo/hardware_profile.h"], draft_context()
    )

    assert photopainter["affected_firmware_profiles"] == [
        "photopainter_release",
        "trusted_lan_photopainter",
        "photopainter_debug",
    ]
    assert "firmware_quick" in photopainter["expensive_gates"]
    assert "firmware_affected" in photopainter["expensive_gates"]
    assert photopainter["firmware_execution_profiles"] == {
        "quick": ["photopainter_release"],
        "affected": ["trusted_lan_photopainter", "photopainter_debug"],
        "full_matrix": [],
    }
    assert shared["affected_firmware_profiles"] == list(FULL_FIRMWARE_PROFILES)
    assert "shared_profile_or_build_surface" in shared["firmware_profile_reasons"]


def test_full_mode_selects_all_supported_firmware_profiles_and_tier_zero():
    plan = build_test_plan(["README.md"], draft_context(draft=False))

    assert plan["firmware_profile_mode"] == "full"
    assert plan["affected_firmware_profiles"] == list(FULL_FIRMWARE_PROFILES)
    assert plan["firmware_execution_profiles"] == {
        "quick": [],
        "affected": [],
        "full_matrix": list(FULL_FIRMWARE_PROFILES),
    }
    assert {"secret_scan", *FULL_EXPENSIVE_GATES} <= set(plan["selected_gates"])
    assert {"ruff", "mypy", "dependency_policy"} <= set(plan["selected_test_suites"])


def test_full_ci_config_mode_requires_actionlint_for_completeness():
    plan = build_test_plan([".github/workflows/ci.yml"], draft_context(draft=False))

    assert "actionlint" in plan["selected_gates"]
    assert plan["full_plan_complete"] is True


def test_full_plan_suite_registry_is_execution_complete():
    plan = build_test_plan(["README.md"], draft_context(draft=False))

    assert set(FULL_PLAN_SUITES) <= set(FULL_SUITE_EXECUTION_OWNERS)
    assert set(FULL_SUITE_EXECUTION_OWNERS.values()) <= FULL_EXECUTION_OWNERS
    assert plan["suite_execution_gaps"] == []
    assert plan["full_suite_execution_gaps"] == []
    assert plan["full_plan_complete"] is True


def test_docker_routing_separates_image_tls_and_storage_boundaries():
    trivy = build_test_plan([".trivyignore"], draft_context())
    dockerfile = build_test_plan(["Dockerfile"], draft_context())
    compose = build_test_plan(["docker-compose.yml"], draft_context())
    tls = build_test_plan([".github/tls-smoke/nginx.conf"], draft_context())

    for plan in (trivy, dockerfile):
        assert "container_security" in plan["expensive_gates"]
        assert "docker_lan_persistence" not in plan["expensive_gates"]
    assert "docker_lan_persistence" in compose["expensive_gates"]
    assert "docker_lan_persistence" not in tls["expensive_gates"]


def test_nas_release_files_route_container_ownership_and_runtime_validation():
    compose = build_test_plan(["docker-compose.nas.yml"], draft_context())
    env = build_test_plan([".env.nas.example"], draft_context())
    updater = build_test_plan(["scripts/update_nas.sh"], draft_context())

    contract = build_test_plan(["nas-deployment-contract.version"], draft_context())
    publish = build_test_plan([".github/workflows/publish-container.yml"], draft_context())
    recovery = build_test_plan(["scripts/create_update_recovery.py"], draft_context())
    preflight = build_test_plan(["inktime/app/core/preflight.py"], draft_context())
    backup = build_test_plan(["inktime/app/services/backups.py"], draft_context())
    contract_test = build_test_plan(
        ["tests/unit/test_container_release_workflow.py"], draft_context()
    )

    for plan in (compose, env, updater, contract, publish, recovery, preflight, backup):
        assert {"docker_runtime_owner", "container_configuration_owner"} <= set(
            plan["selected_test_suites"]
        )
        assert {"nas_update_e2e", "container_security"} <= set(plan["expensive_gates"])
    assert "nas_update_e2e" in contract_test["expensive_gates"]


def test_web_and_mixed_tooling_paths_fail_open_only_outside_known_roots():
    template = build_test_plan(["inktime/app/web/templates/login.html"], draft_context())
    unknown_html = build_test_plan(["unrelated.html"], draft_context())
    pyproject = build_test_plan(["pyproject.toml"], draft_context())

    assert template["ci_mode"] == IMPACT_MODE
    assert "web_ui_owner" in template["selected_test_suites"]
    assert "playwright" in template["expensive_gates"]
    assert unknown_html["ci_mode"] == FULL_MODE
    assert pyproject["ci_mode"] == IMPACT_MODE
    assert "dev_dependencies" in pyproject["changed_domains"]
    assert {"ruff", "mypy", "dependency_policy"} <= set(
        pyproject["selected_test_suites"]
    )
    assert "dependency_audit" not in pyproject["expensive_gates"]
    assert "container_security" not in pyproject["expensive_gates"]


def test_execution_contract_exposes_non_overlapping_impact_and_full_ids():
    impact = build_test_plan(["scripts/runtime_soak.py"], draft_context())
    full = build_test_plan(["scripts/runtime_soak.py"], draft_context(draft=False))

    assert impact["impact_execution_ids"]
    assert impact["full_execution_ids"] == []
    assert full["impact_execution_ids"] == []
    assert full["full_execution_ids"]
    assert impact["execution_overlap"] == []
    assert full["execution_overlap"] == []
    assert impact["execution_id_duplicates"] == []
    assert full["execution_id_duplicates"] == []
    assert impact["suite_gate_name_collisions"] == []


def test_every_production_domain_has_an_owner_or_conservative_full_fallback():
    for path in (
        "inktime/app/api/photos.py",
        "inktime/app/web/templates/login.html",
        "inktime/app/workers/runner.py",
        "inktime/app/db/connection.py",
        "scripts/restore_backup.py",
        "inktime/app/api/devices.py",
        "inktime/app/domain/rendering/release.py",
        "inktime/app/providers/router.py",
        "Dockerfile",
        "scripts/production_tls_smoke.py",
        "esp32/ink-display-7C-photo/ink-display-7C-photo.ino",
        "requirements.txt",
    ):
        plan = build_test_plan([path], draft_context())
        assert plan["production_owner_invariant"] is True, path
        assert plan["owner_suite_gaps"] == {}, path
        assert plan["selected_test_suites"], path


def test_canonical_owner_registry_covers_every_production_domain():
    assert set(PRODUCTION_DOMAINS) == set(DOMAIN_OWNER_SUITES)


def test_provenance_fields_are_preserved_in_canonical_plan():
    plan = build_test_plan(["README.md"], draft_context())

    assert plan["source_head_sha"] == "b" * 40
    assert plan["base_sha"] == "a" * 40
    assert plan["tested_sha"] == "c" * 40
    assert plan["tested_ref_kind"] == "merge-ref"


def test_missing_production_owner_fails_open_to_full(monkeypatch):
    monkeypatch.setitem(DOMAIN_OWNER_SUITES, "python", ())

    plan = build_test_plan(["inktime/app/api/photos.py"], draft_context())

    assert plan["ci_mode"] == FULL_MODE
    assert "production_owner_suite_missing" in plan["why_full_suite"]
    assert plan["full_plan_complete"] is True


def test_cross_layer_integration_regressions_have_explicit_owner_suites():
    expected = {
        "tests/integration/test_analysis_pipeline.py": "provider_analysis_owner",
        "tests/integration/test_ai_cache_singleflight.py": "provider_analysis_owner",
        "tests/integration/test_photo_quality_ai.py": "provider_analysis_owner",
        "tests/integration/test_adaptive_frame_renderer.py": "render_release_owner",
        "tests/integration/test_dual_photo_caption_layout.py": "render_release_owner",
        "tests/integration/test_render_candidate_contract.py": "render_release_owner",
    }

    for path, owner in expected.items():
        plan = build_test_plan([path], draft_context())
        assert owner in plan["selected_test_suites"], path


def test_explicit_full_only_integration_regression_is_documented_and_full():
    plan = build_test_plan(
        ["tests/integration/test_scheduled_release_pipeline.py"], draft_context()
    )

    assert plan["ci_mode"] == FULL_MODE
    assert plan["full_only_test_paths"] == [
        "tests/integration/test_scheduled_release_pipeline.py"
    ]
    assert "full_only_integration_regression" in plan["why_full_suite"]
