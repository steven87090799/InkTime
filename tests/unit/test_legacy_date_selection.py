from __future__ import annotations

import datetime as dt
import importlib
import sys
import types


def _load_renderer(monkeypatch, tmp_path):
    config = types.ModuleType("config")
    config.DB_PATH = str(tmp_path / "photos.db")
    config.BIN_OUTPUT_DIR = str(tmp_path / "output")
    config.FONT_PATH = ""
    config.MEMORY_THRESHOLD = 70
    config.DAILY_PHOTO_QUANTITY = 5
    monkeypatch.setitem(sys.modules, "config", config)
    sys.modules.pop("render_daily_photo", None)
    return importlib.import_module("render_daily_photo")


def test_existing_history_today_selection(monkeypatch, tmp_path):
    renderer = _load_renderer(monkeypatch, tmp_path)
    items = [
        {"path": "a.jpg", "md": "07-17", "memory": 90},
        {"path": "b.jpg", "md": "07-16", "memory": 95},
    ]
    selected, info = renderer.choose_photos_for_today(items, dt.date(2026, 7, 17), count=1)
    assert selected[0]["path"] == "a.jpg"
    assert info["used_md"] == "07-17"
