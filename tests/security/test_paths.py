from __future__ import annotations

from pathlib import Path

import pytest

from inktime.app.core.paths import UnsafePathError, safe_join


def test_normal_child_path(tmp_path):
    base = tmp_path / "photos"
    base.mkdir()
    assert safe_join(base, "2026/holiday.jpg") == base / "2026/holiday.jpg"


@pytest.mark.parametrize(
    "candidate",
    [
        "../secret.jpg",
        "%2e%2e/secret.jpg",
        "%252e%252e/secret.jpg",
        "/etc/passwd",
        "C:\\Windows\\system.ini",
        "..\\secret.jpg",
        "\\\\server\\share\\file.jpg",
    ],
)
def test_traversal_and_absolute_paths_are_rejected(tmp_path, candidate):
    base = tmp_path / "photos"
    base.mkdir()
    with pytest.raises(UnsafePathError):
        safe_join(base, candidate)


def test_similar_prefix_directory_is_rejected(tmp_path):
    base = tmp_path / "photos"
    backup = tmp_path / "photos_backup"
    base.mkdir()
    backup.mkdir()
    with pytest.raises(UnsafePathError):
        safe_join(base, "../photos_backup/private.jpg")


def test_symlink_escape_is_rejected(tmp_path):
    base = tmp_path / "photos"
    outside = tmp_path / "private"
    base.mkdir()
    outside.mkdir()
    (base / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        safe_join(base, "escape/secret.jpg")
