from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from inktime.app.domain.rendering.fonts import (
    BUILTIN_FONTS,
    DEFAULT_FONT_REFERENCE,
    FONT_COMPATIBILITY_TEXT,
    FONT_PREVIEW_TEXT,
    FontCoverageError,
    FontManager,
)


SYSTEM_FONT = Path("/System/Library/Fonts/Supplemental/Arial.ttf")


@pytest.mark.skipif(not SYSTEM_FONT.exists(), reason="測試環境沒有 Arial")
def test_font_coverage_is_checked_instead_of_silent_fallback(tmp_path):
    manager = FontManager(tmp_path / "fonts")
    installed = manager.install(SYSTEM_FONT)
    manager.validate(installed, "InkTime 2026")
    with pytest.raises(FontCoverageError):
        manager.validate(installed, "繁體中文回憶")


def test_builtin_fonts_are_pinned_and_cover_traditional_chinese(tmp_path):
    manager = FontManager(tmp_path / "fonts")
    options = manager.options(DEFAULT_FONT_REFERENCE)

    assert [option.style for option in options[:2]] == ["手寫風格", "文青風格"]
    assert options[0].active is True
    assert all(option.compatible for option in options[:2])
    for index, catalog_font in enumerate(BUILTIN_FONTS):
        option = options[index]
        path = manager.resolve(catalog_font.reference, selectable_only=True)
        assert path.name == option.filename
        assert sha256(path.read_bytes()).hexdigest() == catalog_font.sha256
        manager.validate(path, FONT_PREVIEW_TEXT + FONT_COMPATIBILITY_TEXT)


def test_uploaded_font_reference_stays_inside_managed_directory(tmp_path):
    manager = FontManager(tmp_path / "fonts")

    with pytest.raises(ValueError, match="檔名不合法"):
        manager.resolve("uploaded:../../outside.ttf", selectable_only=True)
    with pytest.raises(ValueError, match="只能選擇"):
        manager.resolve(str(tmp_path.parent / "external.ttf"), selectable_only=True)


def test_install_preserves_name_and_validates_before_atomic_replace(tmp_path):
    manager = FontManager(tmp_path / "fonts")
    source = manager.resolve(DEFAULT_FONT_REFERENCE)
    destination = manager.install(
        source,
        filename="Iansui-Custom.ttf",
        required_text=FONT_COMPATIBILITY_TEXT,
    )

    assert destination.name == "Iansui-Custom.ttf"
    assert manager.reference_for_upload(destination) == "uploaded:Iansui-Custom.ttf"
    manager.validate(destination, FONT_PREVIEW_TEXT)
