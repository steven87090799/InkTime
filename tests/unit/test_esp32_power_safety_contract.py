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


def test_sleep_domains_and_tg28_epd_rail_remain_narrow_and_fail_closed():
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
    assert "type_ = PmicType::TG28" in pmic
    assert "type_ = PmicType::Unknown" in pmic
    assert "writeCommand(" not in pmic
    assert "kAxp2101ChipIdRegister" not in support
    assert "kTg28LdoEnable0 = 0x90" in support
    assert "kTg28Aldo4Voltage = 0x95" in support
    assert "kTg28Aldo4EnableMask = 1U << 3U" in support
    assert "kTg28Aldo4_3300mV = 0x1CU" in support
    assert "kTg28Aldo4ColdStartSettleMs = 500U" in support

    early_pins = _between(
        support,
        "bool holdEpdPinsForColdBoot",
        "class BoundedI2cBus",
    )
    for pin in (
        "board.display.spi.cs",
        "board.display.dc",
        "board.display.reset",
        "board.display.busy",
    ):
        assert pin in early_pins
    assert "board_.buttons.power" not in early_pins
    assert "GPIO_NUM_21" not in early_pins
    assert "gpio_set_level(static_cast<gpio_num_t>(board.display.spi.sck)" not in early_pins
    assert "gpio_set_level(static_cast<gpio_num_t>(board.display.spi.mosi)" not in early_pins
    assert "gpio_set_level(static_cast<gpio_num_t>(board.display.reset), 1)" in early_pins

    constructor = _between(
        support,
        "PhotoPainterSupport::PhotoPainterSupport",
        "PhotoPainterSupport::~PhotoPainterSupport",
    )
    assert "earlyEpdTransportReady_ = prepareSpectra6ColdBootTransport(board_)" in constructor
    assert constructor.index("prepareSpectra6ColdBootTransport(board_)") < constructor.index(
        "holdEpdPinsForColdBoot(board_)"
    )
    assert "earlyEpdPinsReady_ = earlyEpdTransportReady_" in constructor

    prepare = _between(pmic, "bool prepareDisplayPower()", "const char* lastError()")
    assert "voltage & static_cast<uint8_t>(~kTg28AldoVoltageMask)" in prepare
    assert "ldoState | kTg28Aldo4EnableMask" in prepare
    assert "PMIC-EPD-VOLTAGE-READBACK" in prepare
    assert "PMIC-EPD-ENABLE-READBACK" in prepare
    assert "aldo4AlreadyEnabled" in prepare
    assert "kTg28Aldo4ColdStartSettleMs" in prepare
    assert "PMIC-EPD-DISABLE" not in pmic

    hardware = HARDWARE.read_text(encoding="utf-8")
    pmic_types = _between(hardware, "enum class PmicType", "struct SpiPins")
    for pmic_type in ("None", "AXP2101", "TG28", "Unknown"):
        assert pmic_type in pmic_types

    power_sources = _between(hardware, "enum class PowerSourceState", "struct SpiPins")
    for power_source in ("Unknown", "Battery", "Usb"):
        assert power_source in power_sources

    measurements = _between(pmic, "void refreshMeasurements()", "PmicType type()")
    assert measurements.index("powerSourceState_ = PowerSourceState::Unknown") < measurements.index(
        "if (type_ != PmicType::TG28) return;"
    )
    assert measurements.index("kTg28Status1") < measurements.index(
        "PowerSourceState::Usb"
    )
    assert "PowerSourceState::Battery" in measurements
    assert "return powerSourceState_ == PowerSourceState::Usb;" in pmic
    assert "virtual PowerSourceState powerSourceState() const = 0;" in POWER_MANAGER.read_text(
        encoding="utf-8"
    )

    display = _between(
        support,
        "bool PhotoPainterSupport::displayFrame",
        "bool PhotoPainterSupport::displayPairingScreen",
    )
    assert display.index("prepareDisplayPower()") < display.index("impl_->display.begin()")
    assert "impl_->display.safeShutdown()" in display
    assert "releaseDisplayPower()" not in display

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


def test_shared_i2c_bus_uses_open_drain_recovery_before_probe_and_retry():
    support = SUPPORT.read_text(encoding="utf-8")
    recovery = _between(support, "bool recoverI2cBusLines", "class BoundedI2cBus")
    assert "gpio_reset_pin" in recovery
    assert "OUTPUT_OPEN_DRAIN" in recovery
    assert "pulse < 9U" in recovery
    assert "digitalWrite(config.scl, LOW)" in recovery
    assert "digitalWrite(config.sda, LOW)" in recovery
    assert "digitalWrite(config.sda, HIGH)" in recovery
    assert "return digitalRead(config.sda) == HIGH" in recovery
    assert "pinMode(config.sda, OUTPUT)" not in recovery
    assert "pinMode(config.scl, OUTPUT)" not in recovery

    reset = _between(support, "bool resetBus()", "TwoWire& wire_")
    assert reset.index("wire_.end()") < reset.index("recoverI2cBusLines(config_)")
    assert reset.index("recoverI2cBusLines(config_)") < reset.index("wire_.begin(")

    begin = _between(
        support,
        "bool PhotoPainterSupport::begin()",
        "bool PhotoPainterSupport::loadCachedFrame",
    )
    assert begin.index("recoverI2cBusLines(board_.i2c)") < begin.index("Wire.begin(")


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
    usb_to_non_usb = _between(
        service_loop,
        "} else if (servicePowerSource == inktime::PowerSourceState::Usb",
        "servicePowerSource = nextPowerSource;",
    )
    loop_rearm = usb_to_non_usb.index("\n        armMaxAwakeSupervisor();")
    assert usb_to_non_usb.index(
        "nextPowerSource != inktime::PowerSourceState::Usb"
    ) < loop_rearm
    server_stop = usb_service.index("server.stop();")
    final_rearm = usb_service.rindex("\n  armMaxAwakeSupervisor();")
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
    assert "Rev2.0 原理圖都確認 PMIC 為 TG28" in docs
    assert "ALDO4 直接供應" in docs and "`EPD_VCC`" in docs
    assert "`REG95[4:0]`" in docs and "`REG90[3]`" in docs
    assert "韌體不寫 TG28 的 DCDC、充電、全機 shutdown" in docs
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
