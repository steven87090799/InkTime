from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_dependency_policy import (
    pyproject_errors,
    requires_python_accepts_312,
)


VALID_PYPROJECT = """
[project]
name = "inktime"
dynamic = ["version"]
requires-python = ">=3.12,<3.13"

[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[tool.setuptools.dynamic]
version = {attr = "inktime._version.__version__"}
""".lstrip()


def _write_pyproject(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_pyproject_metadata_contract_accepts_current_shape(tmp_path):
    assert pyproject_errors(_write_pyproject(tmp_path, VALID_PYPROJECT)) == []


@pytest.mark.parametrize(
    "specifier",
    [
        ">=3.12,<3.13",
        "==3.12.*",
    ],
)
def test_requires_python_semantically_accepts_python_312(specifier):
    assert requires_python_accepts_312(specifier) is True


@pytest.mark.parametrize(
    "specifier",
    [
        ">=3.11",
        ">=3.100",
        ">=3.12,<3.12",
        ">=3.12,!=3.12.*",
        ">=3.12, definitely-not-a-specifier",
    ],
)
def test_requires_python_semantically_rejects_non_312_or_malformed_specifiers(specifier):
    assert requires_python_accepts_312(specifier) is False


def test_pyproject_metadata_contract_rejects_malformed_runtime_and_build_fields(tmp_path):
    malformed = (
        (
            VALID_PYPROJECT.replace('requires-python = ">=3.12,<3.13"\n', ""),
            "requires-python",
        ),
        (
            VALID_PYPROJECT.replace('requires-python = ">=3.12,<3.13"', 'requires-python = ">=3.11"'),
            "requires-python",
        ),
        (
            VALID_PYPROJECT.replace('requires-python = ">=3.12,<3.13"', 'requires-python = ">=3.100"'),
            "requires-python",
        ),
        (
            VALID_PYPROJECT.replace('requires-python = ">=3.12,<3.13"', 'requires-python = ">=3.12,<3.12"'),
            "requires-python",
        ),
        (
            VALID_PYPROJECT.replace('requires-python = ">=3.12,<3.13"', 'requires-python = ">=3.12,!=3.12.*"'),
            "requires-python",
        ),
        (
            VALID_PYPROJECT.replace(
                'requires-python = ">=3.12,<3.13"',
                'requires-python = ">=3.12, definitely-not-a-specifier"',
            ),
            "requires-python",
        ),
        (
            VALID_PYPROJECT.replace('build-backend = "setuptools.build_meta"\n', ""),
            "build backend",
        ),
        (
            VALID_PYPROJECT.replace(
                'build-backend = "setuptools.build_meta"',
                'build-backend = "flit_core.buildapi"',
            ),
            "build backend",
        ),
        (
            VALID_PYPROJECT.replace('requires = ["setuptools>=61"]\n', ""),
            "build-system.requires",
        ),
        (
            VALID_PYPROJECT.replace('requires = ["setuptools>=61"]', 'requires = ["wheel==0.44.0"]'),
            "build-system.requires",
        ),
        (
            VALID_PYPROJECT.replace('dynamic = ["version"]', "dynamic = []"),
            "dynamic",
        ),
        (
            VALID_PYPROJECT.replace('version = {attr = "inktime._version.__version__"}\n', ""),
            "dynamic version",
        ),
        (
            VALID_PYPROJECT.replace(
                'version = {attr = "inktime._version.__version__"}',
                'version = {attr = "inktime.__version__"}',
            ),
            "dynamic version",
        ),
    )

    for content, expected_error in malformed:
        errors = pyproject_errors(_write_pyproject(tmp_path, content))
        assert any(expected_error in error for error in errors), (expected_error, errors)


@pytest.mark.parametrize("specifier", [">=3.12,<3.13", "==3.12.*"])
def test_pyproject_metadata_contract_accepts_semantic_python_312(tmp_path, specifier):
    content = VALID_PYPROJECT.replace('requires-python = ">=3.12,<3.13"', f'requires-python = "{specifier}"')
    assert pyproject_errors(_write_pyproject(tmp_path, content)) == []


@pytest.mark.parametrize(
    "specifier",
    [">=3.11", ">=3.100", ">=3.12,<3.12", ">=3.12,!=3.12.*", ">=>3.12"],
)
def test_requires_python_rejects_specifiers_that_do_not_accept_python_312(tmp_path, specifier):
    content = VALID_PYPROJECT.replace('requires-python = ">=3.12,<3.13"', f'requires-python = "{specifier}"')
    errors = pyproject_errors(_write_pyproject(tmp_path, content))
    assert any("requires-python" in error for error in errors)


def test_runtime_locks_require_hashes_and_matching_direct_versions(tmp_path):
    from scripts.check_dependency_policy import runtime_lock_errors

    direct = tmp_path / "requirements.txt"
    direct.write_text("requests==2.34.2\n")
    lock = tmp_path / "linux-amd64-py312.txt"
    lock.write_text("requests==2.34.2 --hash=sha256:" + "a" * 64 + "\n")
    assert runtime_lock_errors(lock, direct) == []
    lock.write_text("requests==2.34.2\n")
    assert runtime_lock_errors(lock, direct)
    lock.write_text("requests==2.33.0 --hash=sha256:" + "a" * 64 + "\n")
    assert runtime_lock_errors(lock, direct)
