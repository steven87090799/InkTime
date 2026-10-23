from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from inktime.app.db.migrations import MIGRATIONS
from inktime.app.services.device_releases import DeviceReleaseService
from scripts.ci.persistence_fixture import LAN_SOURCE_ID, seed_lan_release_source


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


def test_lan_source_uses_current_v6_authorization_and_still_rechecks_exclusion(app, tmp_path):
    database = app.extensions["inktime_database"]
    source = tmp_path / "source"
    with database.transaction() as connection:
        digest = seed_lan_release_source(connection, source, runtime_root=str(source))
    manifest = {"render_options": {
        "render_plans": [{"primary_photo_id": LAN_SOURCE_ID, "primary_sha256": digest}],
        "photo_orientations": [{"photo_id": LAN_SOURCE_ID, "orientation": {"rotation_degrees": 0}}],
    }}
    service = DeviceReleaseService(database, tmp_path / "releases")
    assert service._formal_content_is_current(manifest)
    with database.transaction() as connection:
        connection.execute("UPDATE photos SET never_display=1 WHERE id=?", (LAN_SOURCE_ID,))
    assert not service._formal_content_is_current(manifest)
