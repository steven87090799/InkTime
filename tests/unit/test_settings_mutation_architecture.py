from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SETTINGS_API = ROOT / "inktime/app/api/settings.py"
SETTINGS_SERVICE = ROOT / "inktime/app/services/settings_mutation.py"
DEVICE_REPOSITORY = ROOT / "inktime/app/repositories/devices.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(path: Path, name: str) -> ast.FunctionDef:
    for node in _tree(path).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return item
    raise AssertionError(f"missing function: {path}:{name}")


def _called_attributes(node: ast.AST) -> set[str]:
    return {
        child.func.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
    }


def test_settings_api_mutations_use_application_service_boundary():
    source = SETTINGS_API.read_text(encoding="utf-8")
    tree = _tree(SETTINGS_API)
    repository_aliases = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and "inktime_settings_repository" in (ast.get_source_segment(source, node.value) or "")
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    raw_calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"update", "update_many", "rollback"}:
            continue
        owner = ast.get_source_segment(source, node.func.value) or ""
        if "inktime_settings_repository" in owner or owner in repository_aliases:
            raw_calls.append(f"{node.func.attr}:{node.lineno}")
    assert raw_calls == []
    assert "update_many" in _called_attributes(_function(SETTINGS_API, "update_settings"))
    assert "rollback" in _called_attributes(_function(SETTINGS_API, "rollback_settings"))
    assert "update_many" in _called_attributes(_function(SETTINGS_API, "import_settings"))


def test_preset_api_delegates_composite_transaction_without_device_loop():
    function = _function(SETTINGS_API, "apply_preset")
    calls = _called_attributes(function)
    assert "apply_preset_atomic" in calls
    assert "update_render_inputs" not in calls
    assert "transaction" not in calls
    assert not any(isinstance(node, (ast.For, ast.AsyncFor)) for node in ast.walk(function))


def test_application_service_owns_preset_transaction_and_reuses_canonical_cores():
    function = _function(SETTINGS_SERVICE, "apply_preset_atomic")
    calls = _called_attributes(function)
    assert "transaction" in calls
    assert "update_render_inputs_in_transaction" in calls
    assert "update_many_in_transaction" in calls

    device_core = _function(DEVICE_REPOSITORY, "update_render_inputs_in_transaction")
    device_calls = _called_attributes(device_core)
    assert "update" in device_calls
    assert "transaction" not in device_calls


def test_application_service_owns_rollback_preview_and_effect_transaction():
    function = _function(SETTINGS_SERVICE, "rollback")
    calls = _called_attributes(function)
    assert "transaction" in calls
    assert "rollback_preview" in calls
    assert "update_many_in_transaction" in calls


def test_repositories_do_not_import_application_mutation_service():
    for path in (DEVICE_REPOSITORY, ROOT / "inktime/app/repositories/settings.py"):
        imports = {
            node.module
            for node in ast.walk(_tree(path))
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "inktime.app.services.settings_mutation" not in imports
