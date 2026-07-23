from __future__ import annotations

import ast
from pathlib import Path

from PIL import Image

from inktime.app.domain.photos import PhotoPreprocessor


def test_metadata_special_filename_uses_pillow_without_process(monkeypatch, tmp_path):
    filename = "照片 空格 '單引號' \"雙引號\" ; & $ (括號) `反引號`.jpg"
    path = tmp_path / filename
    Image.new("RGB", (640, 480), "white").save(path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Metadata 不得啟動子程序")

    monkeypatch.setattr("subprocess.run", forbidden)
    result = PhotoPreprocessor().analyze(path)
    assert result.width == 640
    assert path.name == filename


def test_production_python_has_no_shell_execution():
    root = Path(__file__).resolve().parents[2] / "inktime"
    violations: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                if (target.value.id, target.attr) in {("os", "system"), ("os", "popen")}:
                    violations.append(f"{path}:{node.lineno}")
                if target.value.id == "subprocess" and any(
                    keyword.arg == "shell"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in node.keywords
                ):
                    violations.append(f"{path}:{node.lineno}")
    assert violations == []
