from __future__ import annotations

from pathlib import Path
from typing import cast

from inktime.app.services.diagnostics import DiagnosticsService


class _VanishingFile:
    def stat(self):
        raise FileNotFoundError("removed between discovery and stat")


def test_diagnostics_file_size_tolerates_vanishing_wal():
    assert DiagnosticsService._file_size(cast(Path, _VanishingFile())) == 0
