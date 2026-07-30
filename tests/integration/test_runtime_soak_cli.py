from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_timeout_cleans_resources_writes_redacted_summary_and_fails(tmp_path):
    summary_path = tmp_path / "summary.json"
    result = subprocess.run(  # noqa: S603 - fixed local interpreter and repository script
        [
            sys.executable,
            str(ROOT / "scripts/runtime_soak.py"),
            "--duration-seconds",
            "60",
            "--max-iterations",
            "100",
            "--timeout-seconds",
            "1",
            "--summary-json",
            str(summary_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "FAIL"
    assert "timeout" in summary["unhandled_exceptions"]
    assert summary["process_exit_status"] == 1
    assert summary["cleanup"]["threads_stopped"] is True
    assert summary["cleanup"]["child_processes_reaped"] is True
    assert summary["cleanup"]["sqlite_connections_closed"] is True
    serialized = json.dumps(summary)
    assert "itd_" not in serialized
    assert "Authorization" not in serialized
    assert "simulation_photos" not in serialized
