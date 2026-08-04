from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HEADER = ROOT / "esp32/ink-display-7C-photo/device_http_transport.h"
TRANSPORT = ROOT / "esp32/ink-display-7C-photo/device_http_transport.cpp"
FIRMWARE = ROOT / "esp32/ink-display-7C-photo/ink-display-7C-photo.ino"


def test_device_ca_contract_is_bounded_and_shared_with_portal():
    header = HEADER.read_text(encoding="utf-8")
    transport = TRANSPORT.read_text(encoding="utf-8")
    firmware = FIRMWARE.read_text(encoding="utf-8")
    assert "kMaxDeviceCaPemBytes = 3500U" in header
    assert "ca.length() <= inktime::kMaxDeviceCaPemBytes" in transport
    assert "maxlength='\");" in firmware
    assert "String(inktime::kMaxDeviceCaPemBytes)" in firmware
    assert "8192" not in transport + firmware


def test_device_save_reports_nvs_write_and_readback_failures_before_restart():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    assert "bool saveConfig" in firmware
    assert "PAIRING-NVS-001" in firmware
    assert "PAIRING-NVS-002" in firmware
    assert "PAIRING-NVS-003" in firmware
    assert 'verify.getString("ca_pem", "")' in firmware
    assert 'verify.getString("hostport", "")' in firmware
    assert 'verify.getString("ssid", "")' in firmware
    assert 'verify.begin("dashcfg", true)' in firmware
    assert "設定未寫入，裝置不會重新啟動" in firmware
