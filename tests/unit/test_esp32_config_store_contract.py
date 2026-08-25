from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "esp32/ink-display-7C-photo/device_config_store_core.h"
HEADER = ROOT / "esp32/ink-display-7C-photo/device_config_store.h"
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
        "pairing_id",
        "pairing_nonce",
        "pairing_expires_at_epoch",
        "pairing_retry_at_epoch",
        "pairing_retry_attempt",
        "tz_offset_minutes",
        "refresh_hour",
        "refresh_minute",
        "rotate180",
        "schedule_count",
        "schedule_slots",
        "prefetch_lead_minutes",
        "delivery_mode",
        "button_wake_action",
        "sync_strategy",
        "sync_time",
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
    assert "schema >= 3U" in core
    assert "schema >= 4U" in core
    assert "kMaxSyncStrategyBytes" in core
    assert "valid_sync_policy" in core
    assert "kMaxPairingNonceBytes" in core


def test_firmware_persists_sync_policy_with_config_version_and_maps_legacy_defaults():
    core = CORE.read_text(encoding="utf-8")
    firmware = FIRMWARE.read_text(encoding="utf-8")
    assert "constexpr uint8_t kPayloadSchemaVersion = 5U" in core
    assert "constexpr uint8_t kLegacyConfigSlots = 12U" in core
    assert "valid_payload_schema" in core
    assert "config_slot_count_for_schema" in core
    assert "payload.sync_strategy = cfg.sync_strategy.c_str();" in firmware
    assert "payload.sync_time = cfg.sync_time.c_str();" in firmware
    assert "cfg.sync_strategy = payload.sync_strategy.c_str();" in firmware
    assert "cfg.sync_time = payload.sync_time.c_str();" in firmware
    assert 'payload.sync_strategy = "first_display_lead"' in core


def test_config_store_reopens_read_only_and_restores_pointer_on_commit_failure():
    store = STORE.read_text(encoding="utf-8")
    assert store.count("verify.begin(storage_namespace_, true)") >= 2
    assert store.count("verify.end();") >= 2
    assert "const auto restorePreviousPointer" in store
    assert "const auto abortCommit" in store
    assert "restorePreviousPointer()" in store
    assert "clearSlot" in store
    assert "JournalPhase::Aborted" in store
    assert "loadAndRepairCanonicalSlot" in store
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


def test_canonical_config_survives_legacy_cleanup_failure_and_records_warning():
    header = HEADER.read_text(encoding="utf-8")
    store = STORE.read_text(encoding="utf-8")
    firmware = FIRMWARE.read_text(encoding="utf-8")
    assert "String* warning = nullptr" in header
    assert "recordCleanupWarning(cleanup_error)" in store
    assert "configStore.load(payload, loadError, &loadWarning)" in firmware
    assert 'lastDeviceWarningCode = "DEVICE-CONFIG-CLEANUP-PENDING"' in firmware
    assert 'payload["warning_code"] = lastDeviceWarningCode' in firmware
    assert 'payload["warning_message"] = lastDeviceWarningMessage' in firmware


def test_firmware_current_version_is_bumped_for_recovery_semantics():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    assert '#define INKTIME_FIRMWARE_VERSION "2.8.2"' in firmware


def test_firmware_status_reports_trusted_device_time_for_replay_ordering():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    assert "appendStatusReportedAt" in firmware
    assert "time(nullptr)" in firmware
    assert "gmtime_r" in firmware
    assert 'strftime(reportedAt, sizeof(reportedAt), "%Y-%m-%dT%H:%M:%SZ"' in firmware
    assert 'payload["status_reported_at"] = reportedAt' in firmware
    assert "epoch < static_cast<time_t>(1600000000)" in firmware


def test_pairing_lifecycle_persists_resume_state_and_uses_confirm_header():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    for marker in (
        "DEVICE_PAIRING_CONFIRM_PATH",
        "auth_state = \"credential_issued\"",
        "pairing_retry_at_epoch",
        "pairingBackoffForAttempt",
        "pairingBackoffSeconds",
        "pairing_recovery_core.h",
        "PairingRetryMetadataStore",
        "pairingRetryNowEpoch",
        "PAIRING-NVS-008",
        "persistRepairPermissionRetry",
        "pairingRetryDue(g_cfg)",
        "X-InkTime-Credential-Version",
        "Authorization\", \"Bearer \" + cfg.device_secret",
        "kPairingPollWindowMs = 30000U",
        "savePairingCandidate(cfg, candidate)",
    ):
        assert marker in firmware
    assert 'candidate.auth_state = "paired"' in firmware
    assert firmware.index("savePairingCandidate(cfg, requestCandidate)") < firmware.index(
        "countedHttpPost(requestHttp, requestBody)"
    )


def test_pairing_request_replay_redraws_pending_display_code():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    assert 'const String requestState = requestResponse["status"] | "pending";' in firmware
    assert "const bool requestPending = requestState == \"pending\";" in firmware
    assert "|| (requestPending && !validCode)" in firmware
    assert "if (requestPending && validCode)" in firmware


def test_schedule_recovery_uses_identity_journal_and_fail_closed_metadata():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    support = SUPPORT.read_text(encoding="utf-8")
    store = STORE.read_text(encoding="utf-8")
    for marker in (
        "reconcilePendingScheduleConfigTransaction",
        "JournalPhase::Prepared",
        "JournalPhase::SchedulePromoted",
        "JournalPhase::ConfigCommitted",
        "JournalPhase::Aborted",
        "kGenericCommitTargetScheduleId",
        "writeJournal",
        "clearJournal",
        "commitPreparedSlot",
        "activeScheduleId()",
        "stagedNextScheduleId()",
        "DEVICE-OFFLINE-SCHEDULE-TXN",
    ):
        assert marker in firmware or marker in support or marker in store
    assert "scheduleIdFromJson" in support
    assert "deserializeJson(document, json)" in support
    assert "rawScheduleId.is<const char*>()" in support
    assert "scheduleId.length() > 128U" in support
    assert "journal.phase == inktime::configstore::JournalPhase::Prepared" in firmware
    assert "!targetScheduleActive" in firmware
    assert "journal.phase != inktime::configstore::JournalPhase::SchedulePromoted" in firmware
    assert "離線排程 active 身分與 recovery target 不一致" in firmware


def test_failed_commit_can_never_activate_candidate_after_reboot():
    store = STORE.read_text(encoding="utf-8")
    assert "aborted.phase = configstore::JournalPhase::Aborted" in store
    assert "const bool restored = restorePreviousPointer()" in store
    assert "loadAndRepairCanonicalSlot" in store
    assert "const char recovery_slot = candidate_wins" in store
    assert "candidate_wins" in store


def test_committed_candidate_survives_pointer_repair():
    store = STORE.read_text(encoding="utf-8")
    assert "journal.phase == configstore::JournalPhase::ConfigCommitted" in store
    assert "verifyPreparedPointer()" in store
    assert "writePointer(\n        promote, prepared_slot, prepared_generation" in store


def test_prepared_journal_restores_previous_config():
    store = STORE.read_text(encoding="utf-8")
    assert "const char recovery_slot = candidate_wins" in store
    assert "journal.prepared_slot : journal.previous_active_slot" in store
    assert "schedule_promotion_pending" in store
    assert "if (!schedule_promotion_pending)" in store


def test_committed_journal_finishes_candidate_activation():
    store = STORE.read_text(encoding="utf-8")
    assert "A committed journal is recovered as candidate-wins" in store
    assert "if (journal.phase == configstore::JournalPhase::ConfigCommitted)" in store
    assert "clearJournal(clear_error)" in store


def test_pointer_corruption_without_journal_fails_safe_to_older_slot():
    store = STORE.read_text(encoding="utf-8")
    core = CORE.read_text(encoding="utf-8")
    assert "findNewest(store, value, error)" in store
    assert "candidateA.generation <= candidateB.generation" in store
    assert "Without a valid pointer or recovery journal, the older complete slot" in store
    assert "phase > static_cast<uint8_t>(JournalPhase::Aborted)" in core
