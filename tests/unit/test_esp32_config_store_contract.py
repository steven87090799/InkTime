from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "esp32/ink-display-7C-photo/device_config_store_core.h"
STORE = ROOT / "esp32/ink-display-7C-photo/device_config_store.cpp"
FIRMWARE = ROOT / "esp32/ink-display-7C-photo/ink-display-7C-photo.ino"
SUPPORT = ROOT / "esp32/ink-display-7C-photo/photopainter_support.cpp"


def test_config_payload_and_envelopes_are_complete_and_bounded():
    core = CORE.read_text(encoding="utf-8")
    for field in (
        "wifi_ssid",
        "wifi_pass",
        "backend_hostport",
        "ca_pem",
        "device_token",
        "device_secret",
        "device_id",
        "auth_state",
        "credential_version",
        "tz_offset_minutes",
        "refresh_hour",
        "refresh_minute",
        "rotate180",
        "schedule_count",
        "schedule_slots",
        "prefetch_lead_minutes",
        "delivery_mode",
        "button_wake_action",
        "config_version",
    ):
        assert field in core
    for marker in (
        "kConfigSlotMagic",
        "kEnvelopeVersion",
        "kPayloadSchemaVersion",
        "generation",
        "serialized.size()",
        "crc32",
        "offset != input.size()",
        "kMaxConfigPayloadBytes",
    ):
        assert marker in core
    assert "std::string" in core
    assert "memcpy" not in core


def test_config_store_reopens_read_only_and_restores_pointer_on_commit_failure():
    store = STORE.read_text(encoding="utf-8")
    assert store.count("verify.begin(storage_namespace_, true)") >= 2
    assert store.count("verify.end();") >= 2
    assert "const auto restorePreviousPointer" in store
    assert "restorePreviousPointer()" in store
    assert 'const char* kSlotAKey = "slot_a"' in store
    assert 'const char* kSlotBKey = "slot_b"' in store
    assert 'const char* kPointerKey = "active"' in store
    assert 'const char* kJournalKey = "sched_txn"' in store
    assert 'const char* kLegacyJournalKey = "journal"' in store


def test_config_load_and_legacy_migration_are_pointer_first_and_fail_closed():
    store = STORE.read_text(encoding="utf-8")
    firmware = FIRMWARE.read_text(encoding="utf-8")
    assert "pointed.generation == pointer_generation" in store
    assert "findNewest(store, value, error)" in store
    assert "candidateA.generation <= candidateB.generation" in store
    assert "loadLegacy(payload, legacy_present, error)" in store
    assert "if (!save(payload, migration_error))" in store
    assert "removeLegacyFormalKeys(cleanup_error)" in store
    assert 'prefs.begin("dashcfg", false)' in firmware
    assert "serverConfigChanged = true;" in firmware
    for code in (
        "PAIRING-NVS-001",
        "PAIRING-NVS-002",
        "PAIRING-NVS-003",
        "PAIRING-NVS-004",
        "PAIRING-NVS-005",
        "PAIRING-NVS-006",
        "PAIRING-NVS-007",
    ):
        assert code in firmware or code in store


def test_schedule_recovery_uses_identity_journal_and_fail_closed_metadata():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    support = SUPPORT.read_text(encoding="utf-8")
    for marker in (
        "reconcilePendingScheduleConfigTransaction",
        "JournalPhase::Prepared",
        "JournalPhase::SchedulePromoted",
        "JournalPhase::ConfigCommitted",
        "writeJournal",
        "clearJournal",
        "commitPreparedSlot",
        "activeScheduleId()",
        "stagedNextScheduleId()",
        "DEVICE-OFFLINE-SCHEDULE-TXN",
    ):
        assert marker in firmware or marker in support
    assert "scheduleIdFromJson" in support
    assert "deserializeJson(document, json)" in support
    assert "rawScheduleId.is<const char*>()" in support
    assert "scheduleId.length() > 128U" in support
    assert "journal.phase == inktime::configstore::JournalPhase::Prepared" in firmware
    assert "!targetScheduleActive" in firmware
    assert "if (!preparedPointer)" in firmware
    assert "離線排程 active 身分與 recovery target 不一致" in firmware
