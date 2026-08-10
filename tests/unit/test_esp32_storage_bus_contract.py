from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "esp32/ink-display-7C-photo/ink-display-7C-photo.ino"
PARTITION_DEFAULT = ROOT / "esp32/ink-display-7C-photo/inktime_default_4M.csv"
PARTITION_PHOTOPAINTER = ROOT / "esp32/ink-display-7C-photo/inktime_photopainter_3M_16MB.csv"
SUPPORT = ROOT / "esp32/ink-display-7C-photo/photopainter_support.cpp"
SUPPORT_HEADER = ROOT / "esp32/ink-display-7C-photo/photopainter_support.h"
SPECTRA = ROOT / "esp32/ink-display-7C-photo/spectra6_73.cpp"
HARDWARE = ROOT / "esp32/ink-display-7C-photo/hardware_profile.h"
DEVICE_API = ROOT / "inktime/app/api/devices.py"


def test_enhanced_offline_converts_once_then_writes_only_formal_frame():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    start = firmware.index("static bool downloadOfflineScheduleSlot")
    end = firmware.index("static bool failOfflineScheduleTransaction", start)
    block = firmware[start:end]

    assert "convertFrame(" in block
    assert "writeFormalFrame(" in block
    assert "convertAndCache(" not in block
    assert block.index("convertFrame(") < block.index("writeFormalFrame(")

    support = SUPPORT.read_text(encoding="utf-8")
    for marker in (
        "makeFormalFramePaths",
        '"/inktime/frames/%s-r%u.itf"',
        '"/inktime/frames/%s-r%u.tmp"',
        '"/inktime/frames/%s-r%u.bak"',
        "validateFormalFrameHeader",
        "SD.rename(temporaryPath, finalPath)",
    ):
        assert marker in support


def test_i2c_retry_is_shared_bounded_and_fail_closed():
    support = SUPPORT.read_text(encoding="utf-8")
    for marker in (
        "class BoundedI2cBus",
        "kI2cMaximumAttempts = 3U",
        "kI2cRetryDelayMs",
        "wire_.end()",
        "wire_.begin(config_.sda, config_.scl, config_.clockHz)",
        "retry_count",
        "bus_reset_count",
        "fail_closed_count",
        "if (!replaySafe || attempt + 1U >= kI2cMaximumAttempts)",
        "ProbePowerManager(BoundedI2cBus&",
        "Shtc3Adapter(BoundedI2cBus&",
        "Pcf85063Adapter(BoundedI2cBus&",
    ):
        assert marker in support
    assert "while (true)" not in support
    assert "for (uint8_t attempt = 0; attempt < kI2cMaximumAttempts; ++attempt)" in support

    header = SUPPORT_HEADER.read_text(encoding="utf-8")
    firmware = FIRMWARE.read_text(encoding="utf-8")
    for marker in (
        "i2cRetryCount()",
        "i2cBusResetCount()",
        "i2cFailClosedCount()",
        'payload["i2c_retry_count"]',
        'payload["i2c_bus_reset_count"]',
        'payload["i2c_fail_closed_count"]',
    ):
        assert marker in header or marker in firmware


def test_formal_frame_gc_has_protection_fences_and_bounded_telemetry():
    support = SUPPORT.read_text(encoding="utf-8")
    for marker in (
        "runFormalFrameGc",
        "kFormalFrameFreeSpaceFloorBytes",
        "kFormalFrameMaximumFiles",
        "kFormalFrameGcMaxDeletesPerWake = 4U",
        "kFormalFrameGcMaxScansPerWake = 32U",
        "slots.size() > kFormalFrameReferenceLimit",
        "scanned < kFormalFrameGcMaxScansPerWake",
        "activeScheduleJson",
        "stagedNextScheduleJson",
        "lastGoodFrameSha256",
        "inFlightFrameSha256",
        "recoveryFrameSha256",
        "protectedFrames.contains(sourceSha256)",
        'String("/inktime/frames/") + entryName',
        "SD.remove(path.c_str())",
        "gcDeletedFiles_",
        "gcDeletedBytes_",
        "gcSkippedProtected_",
    ):
        assert marker in support

    firmware = FIRMWARE.read_text(encoding="utf-8")
    for marker in (
        "runFormalFrameGcForWake();",
        'payload["gc_deleted_files"]',
        'payload["gc_deleted_bytes"]',
        'payload["gc_skipped_protected"]',
    ):
        assert marker in firmware


def test_epd_uses_bounded_buffer_transfer_at_unchanged_four_megahertz():
    spectra = SPECTRA.read_text(encoding="utf-8")
    start = spectra.index("void Spectra6_73::sendData(const uint8_t* data, size_t length)")
    end = spectra.index("void Spectra6_73::hardwareReset", start)
    block = spectra[start:end]
    assert "transferBytes(" in block
    assert "kSpiTransferChunkBytes = 4096U" in spectra
    assert "yield();" in block
    assert "for (size_t offset = 0; offset < length; ++offset)" not in block

    hardware = HARDWARE.read_text(encoding="utf-8")
    assert "4000000" in hardware


def test_status_api_validates_and_persists_phase_four_telemetry():
    api = DEVICE_API.read_text(encoding="utf-8")
    for field in (
        "i2c_retry_count",
        "i2c_bus_reset_count",
        "i2c_fail_closed_count",
        "gc_deleted_files",
        "gc_deleted_bytes",
        "gc_skipped_protected",
    ):
        assert f'"{field}": optional_int("{field}"' in api
        assert f'"{field}": telemetry["{field}"]' in api


def test_queue_ack_journal_is_compact_bounded_crc_checked_and_failure_visible():
    firmware = FIRMWARE.read_text(encoding="utf-8")
    for marker in (
        "AckJournalBlob",
        "AckJournalSnapshotMeta",
        "AckJournalActivePointer",
        "ack_journal_transaction_core.h",
        "AckJournalPreferencesStorage",
        "kAckJournalSnapshotMagic",
        "kAckJournalPointerMagic",
        "kAckJournalBlobMagic",
        "ackJournalCrc",
        "ackJournalSnapshotContentCrc",
        "kMaxAckJournalEntries",
        "ackjournal::commitSnapshot",
        "replacement blob exact readback",
        "snapshot metadata exact readback",
        "active pointer exact readback",
        "legacyAckJournalPresent",
        "removeLegacyAckJournalKeys",
        "server 已接受 ACK，但 local cleanup 失敗",
        "DEVICE-QUEUE-ACK-DURABILITY",
        "journal_.putBytes",
        "journal_.getBytes",
        'lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL"',
        "ACK journal compact blob CRC／readback 驗證失敗",
        "terminalAckEvidence",
        "DEVICE-QUEUE-ACK-JOURNAL-OVERFLOW",
        "DEVICE-QUEUE-ACK-PERMANENT",
        "事件已 quarantine，本輪繼續後續工作",
        "已跳過 AP portal 並等待 bounded recovery wake",
    ):
        assert marker in firmware
    assert "journal.putString(ackJournalKey" not in firmware
    assert 'journal.putUChar("count"' not in firmware


def test_queue_ack_journal_transaction_core_is_host_tested_and_compiled_by_ci():
    core = ROOT / "esp32/ink-display-7C-photo/ack_journal_transaction_core.h"
    budget = ROOT / "esp32/ink-display-7C-photo/ack_journal_storage_budget.h"
    test = ROOT / "tests/firmware/test_ack_journal_transaction_core.cpp"
    budget_test = ROOT / "tests/firmware/test_ack_journal_storage_budget.cpp"
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "class Storage" in core.read_text(encoding="utf-8")
    core_test_text = test.read_text(encoding="utf-8")
    assert "FakeNvs" in core_test_text
    assert "PointerTorn" in core_test_text
    assert "RecordReadbackMismatch" in core_test_text
    assert "expected_count = 3U" in core_test_text
    assert "assertInvalidPointerChoosesOlderCompleteGeneration" in core_test_text
    assert "assertCleanupFailureKeepsPromotedGenerationAuthoritative" in core_test_text
    assert "assertFullJournalFailurePreservesEveryOldRecord" in core_test_text
    assert "assertLegacyBatchAndDuplicateFailureWindows" in core_test_text
    for marker in (
        "LegacyJournalModel",
        "DashCfgModel",
        "BatchPersistenceModel",
        "AuthoritativeCallerModel",
        "durable_claim",
        "retry_requested",
    ):
        assert marker in core_test_text
    assert "empty" in core_test_text.lower()
    budget_text = budget.read_text(encoding="utf-8")
    assert "kTargetNvsPartitionBytes = 0x80000U" in budget_text
    assert "kWorstCaseNvsBytes" in budget_text
    assert "kNvsSafetyMarginBytes" in budget_text
    assert "kMaximumEntries == kMaxAckJournalEntries" in budget_text
    for marker in (
        "cleanupPrevious",
        "allPersistenceSucceeded",
        "legacyCleanupAllowed",
        "retainDuplicateEvidence",
    ):
        assert marker in core.read_text(encoding="utf-8") or marker in FIRMWARE.read_text(encoding="utf-8")
    assert "kTargetNvsPartitionBytes" in budget_test.read_text(encoding="utf-8")
    assert "test_ack_journal_transaction_core.cpp" in workflow
    assert "test_ack_journal_storage_budget.cpp" in workflow


def test_queue_ack_journal_partition_budget_is_source_owned_and_selected_by_ci():
    def partition_row(path: Path, name: str) -> list[str]:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("#") or not line.strip():
                continue
            fields = [field.strip() for field in line.split(",")]
            if fields[0] == name:
                return fields
        raise AssertionError(f"missing partition {name} in {path}")

    def partition_size(path: Path, name: str) -> int:
        return int(partition_row(path, name)[4], 16)

    def partition_offset(path: Path, name: str) -> int:
        return int(partition_row(path, name)[3], 16)

    assert partition_size(PARTITION_DEFAULT, "nvs") == 0x80000
    assert partition_size(PARTITION_PHOTOPAINTER, "nvs") == 0x80000
    assert partition_offset(PARTITION_DEFAULT, "app0") == 0x90000
    assert partition_size(PARTITION_DEFAULT, "app0") == 0x160000
    assert partition_offset(PARTITION_DEFAULT, "app1") == 0x1F0000
    assert partition_size(PARTITION_DEFAULT, "app1") == 0x160000
    assert partition_offset(PARTITION_DEFAULT, "spiffs") == 0x350000
    assert partition_size(PARTITION_DEFAULT, "spiffs") == 0xB0000
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    docs = (ROOT / "docs/devices/WAVESHARE_PHOTOPAINTER_ZH_TW.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs/devices/ESP32_GUIDE_ZH_TW.md").read_text(encoding="utf-8")
    for marker in (
        "inktime_default_4M.csv",
        "inktime_photopainter_3M_16MB.csv",
        "partitions.csv",
    ):
        assert marker in workflow or marker in docs
    assert "select_partition()" in workflow
    assert "automatically selects `partitions.csv`" in guide
    assert "FlashSize=4M" in workflow
    assert "FlashSize=16M,PSRAM=opi,CDCOnBoot=cdc" in workflow
    assert workflow.count("upload.maximum_size=1441792") == 5
    assert workflow.count("upload.maximum_size=3145728") == 3
    assert guide.count("upload.maximum_size=1441792") == 3
    assert docs.count("upload.maximum_size=1441792") == 1
    assert docs.count("upload.maximum_size=3145728") == 2
    assert "PartitionScheme=app3M_fat9M_16MB" not in workflow
    assert "PartitionScheme=app3M_fat9M_16MB" not in docs
    assert "PartitionScheme=inktime_default_4M" not in workflow
    assert "PartitionScheme=inktime_photopainter_3M_16MB" not in workflow
