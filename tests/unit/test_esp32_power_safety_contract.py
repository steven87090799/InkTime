from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "esp32/ink-display-7C-photo/ink-display-7C-photo.ino"
HARDWARE = ROOT / "esp32/ink-display-7C-photo/hardware_profile.h"
SUPPORT = ROOT / "esp32/ink-display-7C-photo/photopainter_support.cpp"


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

    firmware = FIRMWARE.read_text(encoding="utf-8")
    sleep = _between(firmware, "static void goDeepSleepSeconds", "void goDeepSleepMinutes")
    assert "photoPainter.enableWakeSources();" in sleep
    assert "esp_sleep_enable_timer_wakeup(us);" in sleep
    assert sleep.index("photoPainter.enableWakeSources();") < sleep.index(
        "esp_sleep_enable_timer_wakeup(us);"
    )


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
    assert "writeCommand(" not in pmic
    assert "writeRegisters(" not in pmic
    assert "PMIC rail voltage or shutdown-register writes" in pmic

    combined = firmware + support
    for forbidden in (
        "axp_basic_sleep_start",
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

    setup = _between(firmware, "void setup()", "void loop()")
    assert setup.count("startMaxAwakeSupervisor();") == 1
    usb_service = _between(firmware, "bool runUsbServiceMode()", "// =======================\n//  WiFi")
    assert usb_service.index("disarmMaxAwakeSupervisor();") < usb_service.index("while (")
    assert usb_service.index("armMaxAwakeSupervisor();") > usb_service.index("while (")
    portal = _between(firmware, "void startConfigPortal()", "bool runUsbServiceMode()")
    assert "if (usbServiceActive) disarmMaxAwakeSupervisor();" in portal
    assert "armMaxAwakeSupervisor();" in portal


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
    for marker in (
        'g_cfg.button_wake_action == "local_next"',
        "runOfflineLocalCycle(true);",
        "const bool timerWake = wakeCause == ESP_SLEEP_WAKEUP_TIMER;",
        "offlinePrefetchWake(g_cfg, rtcEpoch)",
        "photoPainter.forceNetworkRefresh()",
    ):
        assert marker in setup
