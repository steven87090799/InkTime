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
    assert "ca.length() > inktime::kMaxDeviceCaPemBytes" in transport
    assert "mbedtls_x509_crt_parse" in transport
    assert "DEVICE-TLS-CA-INVALID" in transport
    assert "#ifndef INKTIME_ALLOW_INSECURE_DEVICE_HTTP_HOSTNAMES" in transport
    assert "INKTIME_ALLOW_INSECURE_DEVICE_HTTP_HOSTNAMES 0" in transport
    assert "maxlength='\");" in firmware
    assert "String(inktime::kMaxDeviceCaPemBytes)" in firmware
    assert "ca.length() > 8192" not in transport


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


def test_wake_transport_and_queue_ack_batch_keep_safety_and_power_boundaries():
    header = HEADER.read_text(encoding="utf-8")
    transport = TRANSPORT.read_text(encoding="utf-8")
    firmware = FIRMWARE.read_text(encoding="utf-8")
    assert "bool beginSession" in header
    assert "void closeSession" in header
    assert "secure_client_.stop();" in transport
    assert "HTTPC_DISABLE_FOLLOW_REDIRECTS" in transport
    assert 'DEVICE_QUEUE_ACK_PATH "/api/device/v1/queue/ack"' in firmware
    assert 'DEVICE_QUEUE_ACK_BATCH_PATH "/api/device/v1/queue/acks"' in firmware
    assert "kQueueAckBatchMaxEvents = 8U" in firmware
    assert "persistQueueAckBatch(pending, count)" in firmware
    assert "if (terminalQueueAck(pending[index].event))" in firmware
    assert "allowRetainedTerminal" in firmware
    assert "postQueueAckBatch(cfg, pending + offset, batchCount, true)" in firmware
    assert "if (deviceAuthInvalid) return false;" in firmware
    assert firmware.count("DeviceHttpTransport &fileTransport = wakeHttpTransport") >= 2
    assert "DeviceHttpTransport fileTransport(cfg.ca_pem)" not in firmware
    assert "DeviceHttpTransport &manifestTransport = wakeHttpTransport" in firmware
    assert "DeviceHttpTransport &scheduleTransport = wakeHttpTransport" in firmware
    assert "stopNetworkBeforeDisplay();" in firmware
    assert "WiFi.mode(WIFI_OFF);" in firmware
    assert "esp_wifi_stop();" in firmware


def test_remote_config_persists_candidate_before_runtime_commit():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    start = firmware.index("Config candidate = cfg;")
    end = firmware.index("int width = manifest", start)
    block = firmware[start:end]
    persist = block.index("saveConfig(candidate, &persistError)")
    commit = block.index("cfg = candidate;", persist)
    changed = block.index("serverConfigChanged = true;", commit)
    failure = block.index("if (!saveConfig(candidate, &persistError))")

    assert "setConfigPersistenceError(persistError)" in block
    assert 'lastDeviceErrorCode = "DEVICE-CONFIG-PERSIST"' in firmware
    assert persist < commit < changed
    assert "serverConfigChanged = true;" not in block[failure:commit]
    assert "saveConfig(cfg);" not in block
