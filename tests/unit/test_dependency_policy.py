from __future__ import annotations

from pathlib import Path

from scripts.check_dependency_policy import pyproject_errors


VALID_PYPROJECT = """
[project]
name = "inktime"
dynamic = ["version"]
requires-python = ">=3.10"

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


def test_pyproject_metadata_contract_rejects_malformed_runtime_and_build_fields(tmp_path):
    malformed = (
        (
            VALID_PYPROJECT.replace('requires-python = ">=3.10"\n', ""),
            "requires-python",
        ),
        (
            VALID_PYPROJECT.replace('requires-python = ">=3.10"', 'requires-python = ">=3.11"'),
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
