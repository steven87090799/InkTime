from __future__ import annotations

import importlib
import subprocess
import sys
import types


def _analyzer(monkeypatch):
    config = types.ModuleType("config")
    config.NAS_MOUNT_URL = "smb://secret-user:secret-password@example.invalid/private"
    config.NAS_REMOUNT_TIMEOUT_SEC = 3
    monkeypatch.setitem(sys.modules, "config", config)
    sys.modules.pop("legacy_analyze_photos", None)
    return importlib.import_module("legacy_analyze_photos")


def test_linux_and_docker_never_call_osascript(monkeypatch):
    analyzer = _analyzer(monkeypatch)
    monkeypatch.setattr(analyzer.sys, "platform", "linux")
    monkeypatch.setattr(
        analyzer.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("osascript called")),
    )
    assert analyzer._try_remount_nas() is False


def test_darwin_remount_success_and_safe_log(monkeypatch, capsys):
    analyzer = _analyzer(monkeypatch)
    monkeypatch.setattr(analyzer.sys, "platform", "darwin")
    monkeypatch.setattr(analyzer, "_is_mount_ok", lambda: True)
    monkeypatch.setattr(
        analyzer.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    assert analyzer._try_remount_nas() is True
    output = capsys.readouterr().out
    assert "secret-user" not in output
    assert "secret-password" not in output
    assert "return_code=0" in output


def test_darwin_remount_return_code_failure(monkeypatch, capsys):
    analyzer = _analyzer(monkeypatch)
    monkeypatch.setattr(analyzer.sys, "platform", "darwin")
    monkeypatch.setattr(analyzer, "_is_mount_ok", lambda: False)
    monkeypatch.setattr(
        analyzer.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 12, "", "credential"),
    )
    assert analyzer._try_remount_nas() is False
    output = capsys.readouterr().out
    assert "return_code=12" in output
    assert "credential" not in output


def test_darwin_remount_timeout_is_bounded_and_safe(monkeypatch, capsys):
    analyzer = _analyzer(monkeypatch)
    monkeypatch.setattr(analyzer.sys, "platform", "darwin")
    monkeypatch.setattr(analyzer, "_is_mount_ok", lambda: False)

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("osascript", 3)

    monkeypatch.setattr(analyzer.subprocess, "run", timeout)
    assert analyzer._try_remount_nas() is False
    output = capsys.readouterr().out
    assert "timeout_seconds=3.0" in output
    assert "secret-user" not in output
    assert "secret-password" not in output
