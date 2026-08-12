#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace inktime {
namespace ackjournal {

constexpr uint8_t kMaximumEntries = 32U;

// The transaction core deliberately knows nothing about Preferences/NVS.  The
// firmware adapter supplies exact write/read-back semantics, while host tests
// inject a fake store and fail each phase independently.
struct Snapshot {
  char bank = 0;
  uint64_t generation = 0U;
  std::vector<std::string> records;
};

class Storage {
 public:
  virtual ~Storage() = default;

  // Each write must include an exact read-back check in the concrete adapter.
  virtual bool writeRecord(
      char bank, uint8_t index, const std::string& bytes) = 0;
  virtual bool writeSnapshotMetadata(
      char bank, uint64_t generation,
      const std::vector<std::string>& records) = 0;
  virtual bool writeActivePointer(
      char bank, uint64_t generation, uint8_t count) = 0;
  virtual bool verifyActiveSnapshot(const Snapshot& expected) = 0;

  // Cleanup is deliberately best-effort and is only called after the new
  // pointer and full snapshot have been verified.  A false result must never
  // make an already-promoted snapshot look non-durable.
  virtual bool cleanupPrevious(char bank) = 0;
};

inline bool validBank(char bank) {
  return bank == 'G' || bank == 'H';
}

inline bool allPersistenceSucceeded(bool aggregate, bool current) {
  return aggregate && current;
}

inline bool legacyCleanupAllowed(bool canonical_commit_succeeded) {
  return canonical_commit_succeeded;
}

inline bool retainDuplicateEvidence(
    bool server_result_is_authoritative, bool local_cleanup_succeeded) {
  return server_result_is_authoritative && !local_cleanup_succeeded;
}

inline bool commitSnapshot(
    Storage& storage,
    char previous_bank,
    const Snapshot& next,
    std::string& error
) {
  error.clear();
  if (!validBank(next.bank) || next.bank == previous_bank
      || next.generation == 0U
      || next.records.size() > kMaximumEntries) {
    error = "invalid ACK journal snapshot transaction";
    return false;
  }

  for (size_t index = 0U; index < next.records.size(); ++index) {
    if (!storage.writeRecord(
          next.bank, static_cast<uint8_t>(index), next.records[index])) {
      error = "ACK journal replacement record write/readback failed";
      return false;
    }
  }
  if (!storage.writeSnapshotMetadata(
        next.bank, next.generation, next.records)) {
    error = "ACK journal snapshot metadata write/readback failed";
    return false;
  }
  if (!storage.writeActivePointer(
        next.bank, next.generation,
        static_cast<uint8_t>(next.records.size()))) {
    error = "ACK journal active pointer write/readback failed";
    return false;
  }
  if (!storage.verifyActiveSnapshot(next)) {
    error = "ACK journal active snapshot exact verification failed";
    return false;
  }
  (void)storage.cleanupPrevious(previous_bank);
  return true;
}

}  // namespace ackjournal
}  // namespace inktime
