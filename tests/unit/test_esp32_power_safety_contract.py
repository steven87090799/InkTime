from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "esp32/ink-display-7C-photo/ink-display-7C-photo.ino"
HARDWARE = ROOT / "esp32/ink-display-7C-photo/hardware_profile.h"
SUPPORT = ROOT / "esp32/ink-display-7C-photo/photopainter_support.cpp"
SUPPORT_HEADER = ROOT / "esp32/ink-display-7C-photo/photopainter_support.h"
WAKE_CORE = ROOT / "esp32/ink-display-7C-photo/photopainter_wake_core.h"
POWER_MANAGER = ROOT / "esp32/ink-display-7C-photo/power_manager.h"
DOCS = ROOT / "docs/devices/WAVESHARE_PHOTOPAINTER_ZH_TW.md"


def _between(text: str, start: str, end: str) -> str:
    start_index = text.index(start)
    return text[start_index : text.index(end, start_index)]


def test_photopainter_runtime_key_and_reserved_pin_contracts_are_unchanged():
    hardware = HARDWARE.read_text(encoding="utf-8")
    assert "{kNoPin, 0, 4, 5, true, true}" in hardware
    assert "kBoardConfig.buttons.boot == 0 && kBoardConfig.buttons.user == 4" in hardware
    assert "kBoardConfig.buttons.power == 5" in hardware
    assert "kBoardConfig.buttons.factoryReset == kNoPin" in hardware
    assert "GPIO0 must remain reserved for the PhotoPainter BOOT function" in hardware
    assert "GPIO4 user input must not alias the reserved GPIO5 power button" in hardware


def test_photopainter_ext1_user_wake_validates_gpio4_and_preserves_timer_wake():
    support = SUPPORT.read_text(encoding="utf-8")
    begin = _between(
        support,
        "bool PhotoPainterSupport::begin()",
        "bool PhotoPainterSupport::loadCachedFrame",
    )
    wake = _between(
        support,
        "void PhotoPainterSupport::enableWakeSources()",
        "}  // namespace inktime",
    )
    assert "esp_sleep_enable_ext0_wakeup" not in support
    assert "esp_sleep_enable_ext1_wakeup_io(" in wake
    assert "gpioWakeMask(board_.buttons.user)" in wake
    assert "ESP_EXT1_WAKEUP_ANY_LOW" in wake
    assert "!board_.buttons.userActiveLow" in wake
    assert "wakeCause == ESP_SLEEP_WAKEUP_EXT1" in begin
    assert "esp_sleep_get_ext1_wakeup_status()" in begin
    assert "ext1WakeStatusContainsUserButton(ext1WakeStatus, board_.buttons.user)" in begin
    assert begin.index("rtc_gpio_deinit") < begin.index("pinMode(board_.buttons.user, INPUT_PULLUP)")
    assert begin.index("if (userButtonWake)") < begin.index("wokeFromUserButton_ = true")
    assert "const uint32_t heldMs = millis() - pressedAt;" in begin
    assert "shouldForceNetworkRefresh(heldMs)" in begin
    assert "shouldRequestRecoveryService(heldMs)" in begin

    wake_core = WAKE_CORE.read_text(encoding="utf-8")
    assert "kForceNetworkRefreshHoldMs = 1200U" in wake_core
    assert "kRecoveryServiceHoldMs = 4000U" in wake_core
    assert "kUserButtonHoldMeasurementLimitMs = 5000U" in wake_core
    assert "kRecoveryServiceHoldMs < kUserButtonHoldMeasurementLimitMs" in wake_core
    support_header = SUPPORT_HEADER.read_text(encoding="utf-8")
    assert "bool recoveryServiceRequested() const" in support_header
    assert "bool recoveryServiceRequested_ = false;" in support_header

    firmware = FIRMWARE.read_text(encoding="utf-8")
    sleep = _between(firmware, "static void goDeepSleepSeconds", "void goDeepSleepMinutes")
    assert "photoPainter.enableWakeSources();" in sleep
    assert "esp_sleep_enable_timer_wakeup(us);" in sleep
    assert sleep.index("photoPainter.enableWakeSources();") < sleep.index(
        "esp_sleep_enable_timer_wakeup(us);"
    )


def test_config_is_forward_declared_for_arduino_generated_prototypes():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    forward = firmware.index("struct Config;")
    definition = firmware.index("struct Config {")
    first_config_signature = firmware.index("static bool applyRemoteSchedule")
    assert forward < definition < first_config_signature


def test_sleep_domains_and_pmic_remain_conservative_and_read_only():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    domains = _between(firmware, "void prepareDeepSleepDomains()", "static void goDeepSleepSeconds")
    assert "#if INKTIME_PHOTOPAINTER_ENABLED" in domains
    assert "ESP_PD_DOMAIN_RTC_PERIPH,    ESP_PD_OPTION_AUTO" in domains
    photopainter_branch = _between(
        domains,
        "#if INKTIME_PHOTOPAINTER_ENABLED",
        "#else",
    )
    assert "ESP_PD_OPTION_OFF" not in photopainter_branch

    support = SUPPORT.read_text(encoding="utf-8")
    pmic = _between(support, "class ProbePowerManager", "class Shtc3Adapter")
    assert "chipId != kAxp2101ChipId" in pmic
    assert "type_ = PmicType::Unknown" in pmic
    assert "type_ = PmicType::TG28" not in pmic
    assert pmic.index("chipId != kAxp2101ChipId") < pmic.index("type_ = PmicType::AXP2101")
    assert "writeCommand(" not in pmic
    assert "writeRegisters(" not in pmic
    assert "PMIC rail voltage or shutdown-register writes" in pmic

    hardware = HARDWARE.read_text(encoding="utf-8")
    pmic_types = _between(hardware, "enum class PmicType", "struct SpiPins")
    for pmic_type in ("None", "AXP2101", "TG28", "Unknown"):
        assert pmic_type in pmic_types

    power_sources = _between(hardware, "enum class PowerSourceState", "struct SpiPins")
    for power_source in ("Unknown", "Battery", "Usb"):
        assert power_source in power_sources

    measurements = _between(pmic, "void refreshMeasurements()", "PmicType type()")
    assert measurements.index("powerSourceState_ = PowerSourceState::Unknown") < measurements.index(
        "if (type_ != PmicType::AXP2101) return;"
    )
    assert measurements.index("kAxp2101Status1") < measurements.index(
        "PowerSourceState::Usb"
    )
    assert "PowerSourceState::Battery" in measurements
    assert "return powerSourceState_ == PowerSourceState::Usb;" in pmic
    assert "virtual PowerSourceState powerSourceState() const = 0;" in POWER_MANAGER.read_text(
        encoding="utf-8"
    )

    status = _between(firmware, "void reportDeviceStatus", "void initDisplay")
    assert "powerSourceState != inktime::PowerSourceState::Unknown" in status
    assert 'payload["usb_power"] = powerSourceState == inktime::PowerSourceState::Usb;' in status

    combined = firmware + support
    for forbidden in (
        "axp_basic_sleep_start",
        "disableDC1",
        "disableDC2",
        "disableDC3",
        "disableDC4",
        "disableDC5",
        "disableALDO",
        "disableBLDO",
        "disableDLDO",
        "disableCPUSLDO",
        "setChargingCurrent",
        "setChargingVoltage",
        "setChargeCurrent",
        "setChargeVoltage",
    ):
        assert forbidden not in combined


def test_max_awake_guard_is_independent_bounded_and_usb_exempt():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    supervisor = _between(
        firmware,
        "static constexpr uint32_t kMaxAwakeTimeoutMs",
        "// 實體面板固定",
    )
    assert "10UL * 60UL * 1000UL" in supervisor
    assert "kMaxAwakeSupervisorStackBytes = 2048U" in supervisor
    assert "xTaskNotifyWait" in supervisor
    assert "portMAX_DELAY" in supervisor
    assert "tskIDLE_PRIORITY + 1U" in supervisor
    assert "esp_restart();" in supervisor
    assert supervisor.count("xTaskCreate(") == 1
    assert "maxAwakeSupervisorCreated" in supervisor
    assert "esp_task_wdt" not in firmware

    creation = _between(
        supervisor,
        "static void startMaxAwakeSupervisor()",
        "static void disarmMaxAwakeSupervisor()",
    )
    success = _between(creation, "if (created == pdPASS)", "} else {")
    failure = creation[creation.index("} else {") :]
    assert creation.index("xTaskCreate(") < creation.index("if (created == pdPASS)")
    assert "maxAwakeSupervisorCreated = true;" in success
    assert "maxAwakeSupervisorCreated = false;" in failure

    setup = _between(firmware, "void setup()", "void loop()")
    assert setup.count("startMaxAwakeSupervisor();") == 1
    usb_service = _between(
        firmware,
        "bool runUsbServiceMode(bool explicitRecoveryRequested)",
        "// =======================\n//  WiFi",
    )
    assert "if (!photoPainter.usbConnected()) return false;" not in usb_service
    assert "if (!explicitRecoveryRequested) return false;" in usb_service
    assert "servicePowerSource == inktime::PowerSourceState::Battery" in usb_service
    assert "servicePowerSource == inktime::PowerSourceState::Usb" in usb_service
    assert "servicePowerSource == inktime::PowerSourceState::Unknown" in usb_service
    assert "millis() - serviceStartedMs > AP_TIMEOUT_MS" in usb_service
    assert usb_service.index("PowerSourceState::Usb) {") < usb_service.index(
        "disarmMaxAwakeSupervisor();"
    )
    service_loop = _between(usb_service, "for (;;)", "server.stop();")
    loop_rearm = service_loop.index("armMaxAwakeSupervisor();")
    assert service_loop.index("nextPowerSource != inktime::PowerSourceState::Usb") < loop_rearm
    server_stop = usb_service.index("server.stop();")
    final_rearm = usb_service.rindex("armMaxAwakeSupervisor();")
    assert server_stop < final_rearm < usb_service.index("return true;", server_stop)
    portal = _between(
        firmware,
        "void startConfigPortal()",
        "bool runUsbServiceMode(bool explicitRecoveryRequested)",
    )
    assert "portalPowerSource == inktime::PowerSourceState::Usb" in portal
    assert "nextPowerSource == inktime::PowerSourceState::Battery" in portal
    assert "armMaxAwakeSupervisor();" in portal


def test_explicit_recovery_precedes_normal_display_network_shutdown():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    setup = _between(firmware, "void setup()", "void loop()")
    assert "const bool factoryResetRequested = isFactoryResetRequestedAtBoot();" in setup
    assert "const bool explicitRecoveryRequested = factoryResetRequested" in setup
    recovery_intent = _between(
        setup,
        "const bool explicitRecoveryRequested = factoryResetRequested",
        "#endif",
    )
    assert "photoPainter.recoveryServiceRequested()" in recovery_intent
    assert "photoPainter.forceNetworkRefresh()" not in recovery_intent

    recovery = _between(
        setup,
        "if (explicitRecoveryRequested)",
        "struct tm timeinfo;",
    )
    assert "runUsbServiceMode(explicitRecoveryRequested);" in recovery
    assert "sleepUntilNextSchedule(g_cfg, hasRecoveryTime, recoveryTime);" in recovery
    assert "return;" in recovery

    recovery_call = setup.index("runUsbServiceMode(explicitRecoveryRequested);")
    download = setup.index("downloadDailyPhotoBin(g_cfg)")
    display_shutdown = setup.index("stopNetworkBeforeDisplay();")
    assert recovery_call < download < display_shutdown
    assert "runUsbServiceMode(" not in setup[download:]
    assert "stopNetworkBeforeDisplay();" in setup[download:]

    local_key = _between(
        setup,
        "&& photoPainter.wokeFromUserButton()",
        "runOfflineLocalCycle(true);",
    )
    assert "&& !photoPainter.forceNetworkRefresh()" in local_key
    assert "const bool forcedRefresh = photoPainter.forceNetworkRefresh();" in firmware
    support = SUPPORT.read_text(encoding="utf-8")
    assert "if (forceNetworkRefresh_ ||" in support

    wifi_failure = _between(setup, "if (!connectWiFi(g_cfg))", "startConfigPortal();")
    offline_fallback = _between(
        wifi_failure,
        "if (!explicitRecoveryRequested",
        "runOfflineLocalCycle();",
    )
    assert "activeHasDueFormalSlot(rtcEpoch)" in offline_fallback


def test_photopainter_docs_match_ext1_and_fail_closed_tg28_behavior():
    docs = DOCS.read_text(encoding="utf-8")
    normalized_docs = " ".join(docs.split())
    assert "EXT0 active-low wake" not in docs
    assert "EXT1 `ANY_LOW` active-low wake" in docs
    assert "wake-status" in docs and "GPIO 4" in docs
    assert "1.2 秒但未滿 4 秒" in docs
    assert "持續至少 4 秒才授權" in docs
    assert "PMIC 封裝標示為 TG28" in docs
    assert "不會僅因" in docs and "猜成 TG28" in docs
    assert "TG28／未識別 PMIC 完全不執行 PMIC register mutation" in docs
    assert "不解除 max-awake supervisor" in normalized_docs


def test_unexpected_loop_entry_sleeps_without_network_or_mutation():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    loop = firmware[firmware.index("void loop()") :]
    assert "delay(1000);" in loop
    assert "goDeepSleepSeconds(60U);" in loop
    for forbidden in ("connectWiFi", "display", "saveConfig", "Preferences", "NVS"):
        assert forbidden not in loop


def test_existing_offline_timer_prefetch_and_key_actions_remain_routed():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    setup = _between(firmware, "void setup()", "void loop()")
    local_key = _between(
        setup,
        "&& photoPainter.wokeFromUserButton()",
        "runOfflineLocalCycle(true);",
    )
    assert "&& !photoPainter.forceNetworkRefresh()" in local_key
    for marker in (
        'g_cfg.button_wake_action == "local_next"',
        "runOfflineLocalCycle(true);",
        "const bool timerWake = wakeCause == ESP_SLEEP_WAKEUP_TIMER;",
        "offlinePrefetchWake(g_cfg, rtcEpoch)",
        "photoPainter.forceNetworkRefresh()",
    ):
        assert marker in setup
