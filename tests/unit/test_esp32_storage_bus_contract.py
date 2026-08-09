from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "esp32/ink-display-7C-photo/ink-display-7C-photo.ino"
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
        "kAckJournalBlobMagic",
        "ackJournalCrc",
        "kMaxAckJournalEntries",
        "journal.putBytes",
        "journal.getBytes",
        'journal.putUChar("count"',
        'lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL"',
        "ACK journal NVS blob readback／CRC 失敗",
        "ACK journal count 寫入失敗",
        "terminalAckEvidence",
        "DEVICE-QUEUE-ACK-JOURNAL-OVERFLOW",
        "DEVICE-QUEUE-ACK-PERMANENT",
        "事件已 quarantine，本輪繼續後續工作",
        "已跳過 AP portal 並等待 bounded recovery wake",
    ):
        assert marker in firmware
    assert "journal.putString(ackJournalKey" not in firmware
