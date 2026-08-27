from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HEADER = ROOT / "esp32/ink-display-7C-photo/device_http_transport.h"
TRANSPORT = ROOT / "esp32/ink-display-7C-photo/device_http_transport.cpp"
FIRMWARE = ROOT / "esp32/ink-display-7C-photo/ink-display-7C-photo.ino"
DEV_COMPOSE = ROOT / "docker-compose.dev.yml"
LOCAL_ENV = ROOT / ".env.local.example"
LAN_ENV = ROOT / ".env.lan.production.example"


def test_device_ca_contract_is_bounded_and_shared_with_portal():
    header = HEADER.read_text(encoding="utf-8")
    transport = TRANSPORT.read_text(encoding="utf-8")
    firmware = FIRMWARE.read_text(encoding="utf-8")
    assert "kMaxDeviceCaPemBytes = 3500U" in header
    assert "ca.length() > inktime::kMaxDeviceCaPemBytes" in transport
    assert "mbedtls_x509_crt_parse" in transport
    assert "DEVICE-TLS-CA-INVALID" in transport
    assert "DEVICE_PROFILE == DEVICE_PROFILE_WAVESHARE_PHOTOPAINTER" in header
    assert "#define INKTIME_ALLOW_INSECURE_DEVICE_HTTP 1" in header
    assert "#define INKTIME_ALLOW_INSECURE_DEVICE_HTTP 0" in header
    assert "maxlength='\");" in firmware
    assert "String(inktime::kMaxDeviceCaPemBytes)" in firmware
    assert "ca.length() > 8192" not in transport


def test_trusted_lan_http_is_strictly_rfc1918_and_https_remains_verified():
    transport = TRANSPORT.read_text(encoding="utf-8")
    policy = transport[
        transport.index("bool isRfc1918LiteralHost") : transport.index("bool validCa")
    ]

    assert "first == 10U" in policy
    assert "first == 172U && second >= 16U && second <= 31U" in policy
    assert "first == 192U && second == 168U" in policy
    for forbidden in ("127U", "169U", "ip6addr_aton", "hostname"):
        assert forbidden not in policy
    assert "if (http && !isRfc1918LiteralHost(host))" in transport
    assert 'error_code = "DEVICE-HTTP-PUBLIC-DISALLOWED"' in transport
    assert 'if (https && !validCa(effectiveCa(ca_pem)))' in transport
    assert "secure_client_.setCACert(ca.c_str());" in transport
    assert "setInsecure" not in transport


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
    display = firmware[firmware.index('INK_LOG_INFO("display_refresh_started"') :]
    close_session = display.index("stopNetworkBeforeDisplay();")
    display_init = display.index("initDisplay(g_cfg);", close_session)
    assert close_session < display_init


def test_portal_normalizes_lan_origin_and_keeps_tls_ca_advanced_only():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    page = firmware[firmware.index("String buildConfigPage()") : firmware.index("void handleRoot()")]
    save = firmware[firmware.index("void handleSave()") : firmware.index("void prepareDeepSleepDomains")]

    for label in (
        "InkTime 相框設定",
        "配網資訊",
        "家用 Wi-Fi",
        "InkTime 伺服器",
        "裝置狀態",
        "儲存後，相框會自動連接 InkTime 並顯示配對碼。",
        "畫面旋轉 180°",
        "儲存並連線",
        "進階設定",
    ):
        assert label in page
    assert "@media(max-width:420px)" in page
    assert "min-height:48px" in page
    assert "id='tls_fields'" in page
    assert "tls.hidden=!server.value.trim().toLowerCase().startsWith('https://')" in page
    assert page.index("<summary>進階設定</summary>") < page.index("TLS Root CA")
    for forbidden in ("Secure build", "裝置認證", "Device Secret", "設定網址"):
        assert forbidden not in page
    assert 'host = "http://" + host' in save
    assert "DeviceHttpTransport::backendUrlAllowed" in save


def test_ap_password_is_eight_random_digits_and_shared_by_every_surface():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    generator = firmware[
        firmware.index("static String randomApPassword()") : firmware.index("static void clearConfigNVS")
    ]
    portal = firmware[
        firmware.index("void startConfigPortal()") : firmware.index("bool runUsbServiceMode")
    ]

    assert "esp_random()" in generator
    assert "candidate.length() < 8U" in generator
    assert "sample % 10U" in generator
    assert "candidate == previous" in generator
    assert "randomPortalSecret()" not in portal[portal.index("String apPassword") : portal.index("bool apOk")]
    assert "String apPassword = randomApPassword();" in portal
    assert "portalApPassword = apPassword;" in portal
    assert "WiFi.softAP(apSsid.c_str(), apPassword.c_str())" in portal
    assert "displayPairingScreen(\n      apSsid.c_str(), apPassword.c_str()" in portal
    assert "htmlEscape(portalApPassword)" in firmware
    assert "INK_LOG_INFO(\"pairing_display_ready\", apPassword" not in firmware
    assert "INK_LOG_ERROR(\"pairing_display_failed\", apPassword" not in firmware
    assert "AP_TIMEOUT_MS = 5UL * 60UL * 1000UL" in firmware
    timeout = portal[portal.index("if (millis() - enterMs > AP_TIMEOUT_MS)") :]
    assert "!portalPowerIsUsb" not in timeout


def test_development_compose_can_bind_to_mac_lan_without_changing_production_default():
    development = DEV_COMPOSE.read_text(encoding="utf-8")
    base = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    nas = (ROOT / "docker-compose.nas.yml").read_text(encoding="utf-8")
    local_env = LOCAL_ENV.read_text(encoding="utf-8")
    lan_env = LAN_ENV.read_text(encoding="utf-8")

    assert "${INKTIME_DEV_BIND_ADDRESS:-0.0.0.0}:${INKTIME_PORT:-8765}:8765" in development
    assert "${INKTIME_DEV_PUBLIC_URL:-http://localhost:8765}" in development
    assert "INKTIME_DEV_BIND_ADDRESS=0.0.0.0" in local_env
    assert "INKTIME_DEV_PUBLIC_URL=http://192.168.0.50:8765" in local_env
    assert "${INKTIME_BIND_ADDRESS:-127.0.0.1}" in base
    assert "${INKTIME_BIND_ADDRESS:-127.0.0.1}" in nas
    assert "INKTIME_PUBLIC_URL=http://192.168.1.100:8765" in lan_env
    assert "INKTIME_BIND_ADDRESS=192.168.1.100" in lan_env


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
