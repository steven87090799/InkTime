from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ci import run_selected_suites as runner

from scripts.ci.run_selected_suites import (
    RUNNER_SUITE_TEST_PATHS,
    validate_runner_suite_test_paths,
    selected_runner_suites,
    selected_test_paths,
    shard_test_paths,
)
from scripts.ci.test_plan import (
    FULL_EXECUTION_OWNERS,
    FULL_PLAN_SUITES,
    FULL_SUITE_EXECUTION_OWNERS,
    FULL_ONLY_TEST_PATH_REASONS,
    INTEGRATION_TEST_OWNER_SUITES,
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
        ["ci_planner_contracts", "ci_routing_contracts"]
    )

    assert suites == [
        "ci_planner_contracts",
        "ci_routing_contracts",
    ]
    assert paths[:3] == [
        "tests/unit/test_ci_changed_paths.py",
        "tests/unit/test_ci_test_plan.py",
        "tests/unit/test_ci_selected_suites.py",
    ]
    assert len(paths) == len(set(paths))


def test_directory_mapping_covers_explicit_files_without_repeating_them():
    suites, paths = selected_test_paths(
        ["ci_planner_contracts", "python_application_owner"]
    )

    assert suites == ["ci_planner_contracts", "python_application_owner"]
    assert paths == ["tests/unit", "tests/integration/test_application_factory.py"]


def test_shards_cover_every_selected_file_exactly_once():
    _, paths = selected_test_paths(["python_application_owner", "auth_security_owner"])
    expected = set(shard_test_paths(paths, 1)[0])
    shards = shard_test_paths(paths, 4)
    flattened = [path for shard in shards for path in shard]

    assert set(flattened) == expected
    assert len(flattened) == len(expected)
    assert all(Path(path).is_file() for path in flattened)
    assert shard_test_paths(reversed(paths), 4) == shards


def test_shards_deduplicate_directory_and_explicit_file_overlap():
    paths = ["tests/unit", "tests/unit/test_ci_test_plan.py"]
    flattened = [path for shard in shard_test_paths(paths, 4) for path in shard]
    assert flattened.count("tests/unit/test_ci_test_plan.py") == 1


def test_empty_shard_does_not_fall_back_to_running_all_tests(monkeypatch):
    monkeypatch.setattr(runner.sys, "argv", [
        "runner", "--suites-json", '["ci_planner_contracts"]',
        "--shard-count", "100", "--shard-index", "99",
    ])

    def unexpected_pytest(*args, **kwargs):
        raise AssertionError("Empty shard must not invoke pytest without paths")

    monkeypatch.setattr(runner.subprocess, "run", unexpected_pytest)
    assert runner.main() == 0


@pytest.mark.parametrize("count,index", [(0, 0), (-1, 0), (4, -1), (4, 4)])
def test_invalid_shard_coordinates_fail_closed(monkeypatch, count, index):
    monkeypatch.setattr(runner.sys, "argv", [
        "runner", "--suites-json", '["ci_planner_contracts"]',
        "--shard-count", str(count), "--shard-index", str(index),
    ])
    assert runner.main() == 2


def test_shard_runner_propagates_test_failure_and_requests_timings(monkeypatch):
    monkeypatch.setattr(runner.sys, "argv", [
        "runner", "--suites-json", '["ci_planner_contracts"]',
        "--junit-xml", "artifacts/test.xml",
    ])
    observed = []

    def failed_pytest(command, *, check):
        observed.extend(command)
        assert check is False
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(runner.subprocess, "run", failed_pytest)
    assert runner.main() == 1
    assert "--durations=30" in observed
    assert "--junitxml=artifacts/test.xml" in observed


def test_small_suite_matrix_uses_one_runner(monkeypatch, capsys):
    monkeypatch.setattr(runner.sys, "argv", [
        "runner", "--suites-json", '["ci_planner_contracts"]', "--shard-matrix",
    ])
    assert runner.main() == 0
    assert capsys.readouterr().out.strip() == "[0]"


def test_unknown_suite_fails_closed():
    try:
        selected_runner_suites(["unknown_suite"])
    except ValueError as exc:
        assert "Unknown planner suite" in str(exc)
    else:
        raise AssertionError("unknown planner suite must fail closed")


def test_non_executable_classification_suite_is_not_silently_run():
    assert selected_runner_suites(["docs_contract"]) == []


def test_every_integration_test_is_explicitly_owned_or_full_only():
    integration_paths = {
        str(path)
        for path in Path("tests/integration").glob("test_*.py")
    }
    mapped_paths = {
        path
        for paths in RUNNER_SUITE_TEST_PATHS.values()
        for path in paths
        if path.startswith("tests/integration/") and Path(path).is_file()
    }

    assert integration_paths <= mapped_paths | set(FULL_ONLY_TEST_PATH_REASONS)
    assert set(INTEGRATION_TEST_OWNER_SUITES) <= mapped_paths
    assert set(FULL_ONLY_TEST_PATH_REASONS) - mapped_paths


def test_cross_layer_integration_owner_mapping_is_executable():
    for path, owners in INTEGRATION_TEST_OWNER_SUITES.items():
        for owner in owners:
            assert path in RUNNER_SUITE_TEST_PATHS[owner], (path, owner)
