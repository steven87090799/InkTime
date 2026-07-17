from __future__ import annotations

from pathlib import Path

import pytest

from inktime.app.domain.rendering.fonts import FontCoverageError, FontManager


SYSTEM_FONT = Path("/System/Library/Fonts/Supplemental/Arial.ttf")


@pytest.mark.skipif(not SYSTEM_FONT.exists(), reason="測試環境沒有 Arial")
def test_font_coverage_is_checked_instead_of_silent_fallback(tmp_path):
    manager = FontManager(tmp_path / "fonts")
    installed = manager.install(SYSTEM_FONT)
    manager.validate(installed, "InkTime 2026")
    with pytest.raises(FontCoverageError):
        manager.validate(installed, "繁體中文回憶")
