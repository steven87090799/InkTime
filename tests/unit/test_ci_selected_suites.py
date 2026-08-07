from scripts.ci.run_selected_suites import (
    RUNNER_SUITE_TEST_PATHS,
    selected_runner_suites,
    selected_test_paths,
)
from scripts.ci.test_plan import (
    FULL_PLAN_SUITES,
    SELECTED_SUITE_RUNNER,
    SUITE_EXECUTION_OWNERS,
)


def test_every_full_plan_suite_has_an_execution_owner():
    assert set(FULL_PLAN_SUITES) <= set(SUITE_EXECUTION_OWNERS)


def test_every_selected_runner_suite_has_executable_paths():
    expected = {
        suite
        for suite, owner in SUITE_EXECUTION_OWNERS.items()
        if owner == SELECTED_SUITE_RUNNER
    }

    assert expected == set(RUNNER_SUITE_TEST_PATHS)
    for paths in RUNNER_SUITE_TEST_PATHS.values():
        assert paths


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
