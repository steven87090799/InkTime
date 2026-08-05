import pytest

from scripts.ci.changed_paths import _validate_commit_sha, classify_paths


def test_commit_sha_validator_accepts_lowercase_and_uppercase_hex():
    lowercase = "0123456789abcdef" * 2 + "01234567"
    uppercase = "ABCDEF" * 6 + "ABCD"

    assert _validate_commit_sha(lowercase, "base") == lowercase
    assert _validate_commit_sha(uppercase, "head") == uppercase


@pytest.mark.parametrize(
    "value",
    [
        "a" * 39,
        "HEAD",
        "fix/ci-routing-and-nonblocking-delivery",
        "g" * 40,
        "a" * 39 + "!",
    ],
)
def test_commit_sha_validator_rejects_non_complete_hex_sha(value):
    with pytest.raises(ValueError):
        _validate_commit_sha(value, "head")


def test_readme_changes_are_docs_only():
    flags = classify_paths(["README.md"])

    assert flags["docs"] is True
    assert flags["full_suite"] is False
    assert flags["python"] is False
    assert flags["docker"] is False
    assert flags["firmware"] is False


def test_web_template_changes_route_web_ui_only():
    flags = classify_paths(["inktime/app/web/templates/devices.html"])

    assert flags["web_ui"] is True
    assert flags["python"] is False
    assert flags["full_suite"] is False


def test_management_ui_and_ordinary_backend_are_python_and_ui_or_python_only():
    management = classify_paths(["tests/integration/test_management_ui.py"])
    backend = classify_paths(["inktime/app/api/photos.py"])

    assert management["python"] is True
    assert management["web_ui"] is True
    assert backend["python"] is True
    assert backend["web_ui"] is False


def test_auth_session_and_csrf_changes_route_browser_security_contracts():
    flags = classify_paths(["inktime/app/api/session.py"])

    assert flags["python"] is True
    assert flags["web_ui"] is True
    assert flags["tls_security"] is True


def test_python_and_dependency_changes_route_expected_tiers():
    flags = classify_paths(["inktime/app/services/analysis.py"])
    dependency_flags = classify_paths(["requirements.txt"])

    assert flags["python"] is True
    assert flags["provider_ai"] is True
    assert flags["full_suite"] is False
    assert dependency_flags["dependencies"] is True
    assert dependency_flags["full_suite"] is True


def test_scheduler_and_migration_changes_route_runtime_and_persistence():
    scheduler = classify_paths(["inktime/app/workers/scheduler.py"])
    migration = classify_paths(["inktime/app/db/migrations.py"])

    assert scheduler["python"] is True
    assert scheduler["runtime"] is True
    assert migration["python"] is True
    assert migration["runtime"] is True
    assert migration["persistence"] is True


def test_tls_proxy_changes_route_tls_and_container_contracts():
    flags = classify_paths([".github/tls-smoke/nginx.conf"])

    assert flags["tls_security"] is True
    assert flags["docker"] is True
    assert flags["ci_config"] is True
    assert flags["full_suite"] is True


def test_specialised_surfaces_route_their_expensive_contracts():
    flags = classify_paths(
        [
            "esp32/ink-display-7C-photo/ink-display-7C-photo.ino",
            "Dockerfile",
            "tests/e2e/test_login.py",
            "scripts/benchmark_models.py",
        ]
    )

    assert flags["firmware"] is True
    assert flags["docker"] is True
    assert flags["web_ui"] is True
    assert flags["benchmark"] is True
    assert flags["full_suite"] is False


def test_device_manifest_backend_contract_routes_python_and_firmware():
    flags = classify_paths(["inktime/app/api/devices.py"])

    assert flags["python"] is True
    assert flags["firmware"] is True


def test_agents_policy_is_ci_config_and_full_suite():
    flags = classify_paths(["AGENTS.md"])

    assert flags["ci_config"] is True
    assert flags["full_suite"] is True


def test_unknown_paths_fail_open_to_full_suite():
    flags = classify_paths(["new-production-surface.toml"])

    assert flags["changed"] is True
    assert flags["full_suite"] is True


def test_ci_policy_changes_force_full_suite():
    flags = classify_paths([".github/workflows/ci.yml"])

    assert flags["ci_config"] is True
    assert flags["full_suite"] is True


def test_manual_full_suite_is_explicit():
    flags = classify_paths(["docs/README.md"], force_full_suite=True)

    assert flags["full_suite"] is True
