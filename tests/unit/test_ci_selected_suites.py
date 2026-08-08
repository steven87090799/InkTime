from scripts.ci.run_selected_suites import (
    RUNNER_SUITE_TEST_PATHS,
    validate_runner_suite_test_paths,
    selected_runner_suites,
    selected_test_paths,
)
from scripts.ci.test_plan import (
    FULL_EXECUTION_OWNERS,
    FULL_PLAN_SUITES,
    FULL_SUITE_EXECUTION_OWNERS,
    IMPACT_EXECUTION_OWNERS,
    NON_EXECUTABLE_SUITES,
    SELECTED_SUITE_RUNNER,
    SUITE_EXECUTION_OWNERS,
)


def test_every_full_plan_suite_has_an_execution_owner():
    assert set(FULL_PLAN_SUITES) <= set(FULL_SUITE_EXECUTION_OWNERS)
    assert set(FULL_SUITE_EXECUTION_OWNERS.values()) <= FULL_EXECUTION_OWNERS
    assert "docs_contract" in NON_EXECUTABLE_SUITES


def test_impact_execution_registry_points_at_real_jobs_or_selected_runner():
    assert set(SUITE_EXECUTION_OWNERS.values()) <= IMPACT_EXECUTION_OWNERS


def test_every_selected_runner_suite_has_executable_paths():
    expected = {
        suite
        for suite, owner in SUITE_EXECUTION_OWNERS.items()
        if owner == SELECTED_SUITE_RUNNER
    }

    assert expected == set(RUNNER_SUITE_TEST_PATHS)
    for paths in RUNNER_SUITE_TEST_PATHS.values():
        assert paths


def test_every_runner_mapping_path_exists_and_contains_tests():
    assert validate_runner_suite_test_paths() == []


def test_selected_paths_are_deduplicated_and_ordered():
    suites, paths = selected_test_paths(
        ["ci_planner_contracts", "ci_routing_contracts", "python_application_owner"]
    )

    assert suites == [
        "ci_planner_contracts",
        "ci_routing_contracts",
        "python_application_owner",
    ]
    assert paths[:3] == [
        "tests/unit/test_ci_changed_paths.py",
        "tests/unit/test_ci_test_plan.py",
        "tests/unit/test_ci_selected_suites.py",
    ]
    assert len(paths) == len(set(paths))


def test_unknown_suite_fails_closed():
    try:
        selected_runner_suites(["unknown_suite"])
    except ValueError as exc:
        assert "Unknown planner suite" in str(exc)
    else:
        raise AssertionError("unknown planner suite must fail closed")


def test_non_executable_classification_suite_is_not_silently_run():
    assert selected_runner_suites(["docs_contract"]) == []
