from __future__ import annotations

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE_DIRECTORY = ROOT / "esp32/ink-display-7C-photo"


def _section(source: str, start: str, end: str) -> str:
    offset = source.index(start)
    return source[offset:source.index(end, offset)]


def test_actual_sketch_preserves_last_success_after_refresh_and_storage_failures(tmp_path):
    """Execute the owning sketch functions with only NVS/panel IO substituted."""
    compiler = shutil.which("g++") or shutil.which("clang++")
    assert compiler is not None, "Hosted firmware persistence regression requires a C++ compiler"
    sketch = (FIRMWARE_DIRECTORY / "ink-display-7C-photo.ino").read_text()
    types = (FIRMWARE_DIRECTORY / "queue_runtime_types.h").read_text()
    functions = [_section(types, "struct StoredDisplayRecord", "struct PendingQueueAck")]
    boundaries = [
        ("struct __attribute__((packed)) DisplayRecordBlob", "static StoredDisplayRecord loadDisplayRecord"),
        ("static StoredDisplayRecord loadDisplayRecord", "#if INKTIME_PHOTOPAINTER_ENABLED\nstatic bool runFormalFrameGcForWake"),
        ("static bool beginPanelMutation", "#if INKTIME_PHOTOPAINTER_ENABLED\nstatic bool displayPowerStatusScreenSafely"),
        ("static bool runFormalFrameGcForWake", "#endif\n\nstatic bool beginPanelMutation"),
        ("static void saveDisplayRecord", "#if INKTIME_PHOTOPAINTER_ENABLED\nstatic bool restoreLastSuccessfulPhoto"),
        ("static bool restoreLastSuccessfulPhoto", "#endif\n\nstatic bool shouldSkipCurrentDisplay"),
        ("static bool shouldSkipCurrentDisplay", "static bool validPendingQueueAck"),
    ]
    functions.extend(_section(sketch, start, end) for start, end in boundaries)
    harness = (ROOT / "tests/fixtures/firmware/display_record_harness.cpp").read_text()
    translation_unit = tmp_path / "display_record.cpp"
    translation_unit.write_text(harness.replace("/* SKETCH_FUNCTIONS */", "\n".join(functions)))
    executable = tmp_path / "display_record"
    built = subprocess.run(  # noqa: S603 - compile the repository-owned fixture in an isolated directory
        [compiler, "-std=c++17", "-DINKTIME_PHOTOPAINTER_ENABLED=1", "-I", str(FIRMWARE_DIRECTORY),
         str(translation_unit), "-o", str(executable)],
        capture_output=True, text=True, check=False,
    )
    assert built.returncode == 0, built.stderr
    result = subprocess.run(  # noqa: S603 - execute only the fixture compiled above
        [str(executable)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
