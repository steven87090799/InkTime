"""Small context-tool contracts; executed by Hosted CI, not local pytest."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ai_context", ROOT / "scripts/ci/ai_context.py")
assert SPEC is not None and SPEC.loader is not None
context_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(context_tool)


def test_default_limit_and_legacy_aliases():
    assert context_tool._parser().parse_args(["rendering"]).max_items == 12
    index = json.loads(context_tool.INDEX_PATH.read_text(encoding="utf-8"))
    for alias, target in index["route_aliases"].items():
        assert context_tool._route(index, alias)["id"] == target


def test_directory_hint_does_not_enumerate_or_read_sources(tmp_path):
    directory = tmp_path / "services"
    directory.mkdir()
    with patch.object(context_tool, "ROOT", tmp_path), patch.object(
        Path, "rglob", side_effect=AssertionError("recursive inventory")
    ), patch.object(Path, "read_text", side_effect=AssertionError("source read")):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            context_tool._print_entrypoint("services", 12)
    assert len(output.getvalue().splitlines()) == 3
    assert "next: rg" in output.getvalue()


def test_oversized_route_sections_are_bounded(tmp_path):
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps({"task_routes": [{
        "id": "wide", "entrypoints": [f"file{i}.py" for i in range(100)],
        "contracts": [f"contract{i}.md" for i in range(100)],
        "tests": [f"test{i}.py" for i in range(100)],
    }]}), encoding="utf-8")
    output = io.StringIO()
    with patch.object(context_tool, "INDEX_PATH", index_path), patch.object(
        context_tool, "_print_entrypoint"
    ) as entrypoint, patch("sys.argv", ["ai_context.py", "wide"]):
        with contextlib.redirect_stdout(output):
            assert context_tool.main() == 0
    assert entrypoint.call_count == 12
    assert output.getvalue().count("  - ") == 24
