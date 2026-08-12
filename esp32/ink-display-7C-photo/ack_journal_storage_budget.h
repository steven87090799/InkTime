#pragma once

#include <cstddef>

#include "ack_journal_transaction_core.h"
#include "device_config_store_core.h"
#include "offline_schedule_core.h"
#include "queue_client_core.h"

namespace inktime {
namespace ackjournal {

// This is the repository-owned partition contract used by the ESP32 7C
// firmware builds.  The matching CSV keeps the two COW banks, config A/B,
// retry metadata, and migration headroom in the same NVS partition.  The
// stock 20 KiB app3M_fat9M_16MB table is intentionally not sufficient for the
// 32-entry at-least-once ACK contract.
constexpr size_t kTargetNvsPartitionBytes = 0x80000U;
constexpr size_t kNvsEntryOverheadBytes = 64U;
constexpr size_t kNvsSafetyMarginBytes = 0x40000U;

constexpr size_t kAckJournalBlobBytes =
  4U + 1U + 1U + 4U + 1U + 1U + 8U + 2U + 2U + 2U
  + (kQueueIdentifierMaxBytes + 1U)
  + 65U
  + (kQueueIdentifierMaxBytes + 1U)
  + 4U;
constexpr size_t kAckJournalSnapshotMetaBytes = 24U;
constexpr size_t kAckJournalActivePointerBytes = 20U;

// Two canonical COW banks plus one surviving legacy generation are counted
// simultaneously.  This is a peak bound, not a steady-state estimate.
constexpr size_t kAckJournalPeakRecordBytes =
  3U * kMaxAckJournalEntries * kAckJournalBlobBytes
  + 2U * kAckJournalSnapshotMetaBytes
  + kAckJournalActivePointerBytes;
constexpr size_t kAckJournalPeakNvsBytes =
  kAckJournalPeakRecordBytes
  + (3U * kMaxAckJournalEntries + 3U) * kNvsEntryOverheadBytes;

// The legacy field representation has eight independent values per entry.
// Keep both its compact-blob and field forms in the migration bound because
// upgrades must not delete legacy evidence until canonical read-back passes.
constexpr size_t kLegacyAckJournalFieldEntryBytes =
  (kQueueIdentifierMaxBytes + 1U + kNvsEntryOverheadBytes)
  + (4U + kNvsEntryOverheadBytes)
  + (1U + kNvsEntryOverheadBytes)
  + (1U + kNvsEntryOverheadBytes)
  + (65U + kNvsEntryOverheadBytes)
  + (1U + kNvsEntryOverheadBytes)
  + (kQueueIdentifierMaxBytes + 1U + kNvsEntryOverheadBytes)
  + (8U + kNvsEntryOverheadBytes);
constexpr size_t kLegacyAckJournalPeakNvsBytes =
  kMaxAckJournalEntries * (kAckJournalBlobBytes + kNvsEntryOverheadBytes)
  + kMaxAckJournalEntries * kLegacyAckJournalFieldEntryBytes
  + 2U * kNvsEntryOverheadBytes;

constexpr size_t kConfigStorePeakNvsBytes =
  2U * (configstore::kMaxConfigPayloadBytes + 64U + kNvsEntryOverheadBytes)
  + 32U + kNvsEntryOverheadBytes;
constexpr size_t kLargeCaPeakNvsBytes =
  configstore::kMaxCaPemBytes + kNvsEntryOverheadBytes;
constexpr size_t kRetryAndPairingMetadataPeakNvsBytes = 8192U;

constexpr size_t kWorstCaseNvsBytes =
  kAckJournalPeakNvsBytes
  + kLegacyAckJournalPeakNvsBytes
  + kConfigStorePeakNvsBytes
  + kLargeCaPeakNvsBytes
  + kRetryAndPairingMetadataPeakNvsBytes
  + kNvsSafetyMarginBytes;

static_assert(kMaximumEntries == kMaxAckJournalEntries,
              "ACK transaction and firmware logical capacities must match");
static_assert(kWorstCaseNvsBytes <= kTargetNvsPartitionBytes,
              "ACK/config NVS peak exceeds the repository partition contract");

}  // namespace ackjournal
}  // namespace inktime
