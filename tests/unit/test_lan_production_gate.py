from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from inktime.app.db.migrations import MIGRATIONS


ROOT_DIR = Path(__file__).resolve().parents[2]


def test_lan_production_gate_loads_without_application_site_packages():
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and repository-owned script.
        [
            sys.executable,
            "-S",
            "-c",
            (
                "import runpy; "
                "gate = runpy.run_path('scripts/lan_production_gate.py'); "
                "print(gate['EXPECTED_MIGRATION_VERSION'])"
            ),
        ],
        cwd=ROOT_DIR,
        check=True,
        capture_output=True,
        text=True,
    )

    # Compare the dependency-free script with the application's migration registry.
    expected_version = max(migration.version for migration in MIGRATIONS)
    assert completed.stdout.strip() == str(expected_version)
