#include <cassert>
#include <cstdint>
#include <initializer_list>
#include <map>
#include <string>
#include <vector>

#include "ack_journal_transaction_core.h"

using inktime::ackjournal::Snapshot;

class FakeNvs final : public inktime::ackjournal::Storage {
 public:
  enum class Fault {
    None,
    FirstRecord,
    MiddleRecord,
    LastRecord,
    RecordReadbackMismatch,
    Metadata,
    MetadataReadbackMismatch,
    Pointer,
    PointerTorn,
    Verify,
    Cleanup,
  };

  struct Bank {
    uint64_t generation = 0U;
    bool metadata_valid = false;
    std::vector<std::string> records;
  };

  bool writeRecord(char bank, uint8_t index, const std::string& bytes) override {
    if (fault == Fault::FirstRecord && index == 0U) return false;
    if (fault == Fault::MiddleRecord && index == 1U) return false;
    if (fault == Fault::LastRecord && index + 1U == expected_count) return false;
    Bank& target = banks[bank];
    if (target.records.size() <= index) target.records.resize(index + 1U);
    target.records[index] = bytes;
    if (fault == Fault::RecordReadbackMismatch) {
      target.records[index].push_back('!');
      return false;
    }
    return true;
  }

  bool writeSnapshotMetadata(
      char bank, uint64_t generation,
      const std::vector<std::string>& records) override {
    if (fault == Fault::Metadata) return false;
    banks[bank].generation = generation;
    banks[bank].records.resize(records.size());
    banks[bank].metadata_valid = true;
    if (fault == Fault::MetadataReadbackMismatch) {
      banks[bank].generation += 1U;
      return false;
    }
    return true;
  }

  bool writeActivePointer(char bank, uint64_t generation, uint8_t count) override {
    if (fault == Fault::Pointer) return false;
    if (fault == Fault::PointerTorn) {
      pointer_valid = false;
      return false;
    }
    active_bank = bank;
    active_generation = generation;
    active_count = count;
    pointer_valid = true;
    return true;
  }

  bool verifyActiveSnapshot(const Snapshot& expected) override {
    if (fault == Fault::Verify) return false;
    if (!pointer_valid || active_bank != expected.bank
        || active_generation != expected.generation
        || active_count != expected.records.size()) return false;
    const Bank& active = banks.at(active_bank);
    return active.metadata_valid && active.generation == expected.generation
      && active.records == expected.records;
  }

  bool cleanupPrevious(char bank) override {
    cleanup_called = true;
    if (fault == Fault::Cleanup) return false;
    banks.erase(bank);
    return true;
  }

  Snapshot rebootRead() const {
    const Bank* pointed = pointer_valid ? bank(active_bank) : nullptr;
    if (pointed != nullptr && pointed->metadata_valid
        && pointed->generation == active_generation
        && pointed->records.size() == active_count) {
      return snapshotFrom(active_bank, *pointed);
    }

    const Bank* candidate_g = completeBank('G');
    const Bank* candidate_h = completeBank('H');
    if (candidate_g != nullptr && candidate_h != nullptr) {
      // Invalid pointer recovery is deliberately fail-safe.  The newer bank
      // may be a complete candidate whose pointer promotion tore; replaying
      // from the older generation is safer than losing an ACK forever.
      return candidate_g->generation <= candidate_h->generation
        ? snapshotFrom('G', *candidate_g)
        : snapshotFrom('H', *candidate_h);
    }
    if (candidate_g != nullptr) return snapshotFrom('G', *candidate_g);
    if (candidate_h != nullptr) return snapshotFrom('H', *candidate_h);
    return {};
  }

  void seed(const Snapshot& snapshot) {
    banks.clear();
    seedBank(snapshot);
    active_bank = snapshot.bank;
    active_generation = snapshot.generation;
    active_count = static_cast<uint8_t>(snapshot.records.size());
    pointer_valid = true;
  }

  void seedBank(const Snapshot& snapshot) {
    Bank& target = banks[snapshot.bank];
    target.generation = snapshot.generation;
    target.records = snapshot.records;
    target.metadata_valid = true;
  }

  void corruptPointer() { pointer_valid = false; }

  Fault fault = Fault::None;
  uint8_t expected_count = 0U;
  bool cleanup_called = false;

 private:
  static Snapshot snapshotFrom(char bank, const Bank& value) {
    Snapshot snapshot;
    snapshot.bank = bank;
    snapshot.generation = value.generation;
    snapshot.records = value.records;
    return snapshot;
  }

  const Bank* bank(char name) const {
    const auto found = banks.find(name);
    return found == banks.end() ? nullptr : &found->second;
  }

  const Bank* completeBank(char name) const {
    const Bank* candidate = bank(name);
    return candidate != nullptr && candidate->metadata_valid
        && candidate->records.size() <= inktime::ackjournal::kMaximumEntries
      ? candidate
      : nullptr;
  }

  std::map<char, Bank> banks;
  char active_bank = 0;
  uint64_t active_generation = 0U;
  uint8_t active_count = 0U;
  bool pointer_valid = false;
};

static Snapshot makeSnapshot(
    char bank, uint64_t generation, std::initializer_list<const char*> values) {
  Snapshot snapshot;
  snapshot.bank = bank;
  snapshot.generation = generation;
  for (const char* value : values) snapshot.records.emplace_back(value);
  return snapshot;
}

static Snapshot makeBoundedSnapshot(char bank, uint64_t generation, size_t count) {
  Snapshot snapshot;
  snapshot.bank = bank;
  snapshot.generation = generation;
  for (size_t index = 0U; index < count; ++index) {
    snapshot.records.emplace_back("ACK-" + std::to_string(index));
  }
  return snapshot;
}

static void assertPreviousSnapshotAfterFault(FakeNvs::Fault fault) {
  FakeNvs nvs;
  const Snapshot previous = makeSnapshot('G', 7U, {"A", "B", "C"});
  nvs.seed(previous);
  nvs.expected_count = 3U;
  nvs.fault = fault;
  const Snapshot replacement = makeSnapshot('H', 8U, {"A2", "B2", "C2"});
  std::string error;
  assert(!inktime::ackjournal::commitSnapshot(nvs, previous.bank, replacement, error));
  const Snapshot afterReboot = nvs.rebootRead();
  if (fault == FakeNvs::Fault::Verify) {
    assert(afterReboot.bank == replacement.bank);
    assert(afterReboot.generation == replacement.generation);
    assert(afterReboot.records == replacement.records);
  } else {
    assert(afterReboot.bank == previous.bank);
    assert(afterReboot.generation == previous.generation);
    assert(afterReboot.records == previous.records);
  }
}

static void assertInvalidPointerChoosesOlderCompleteGeneration() {
  FakeNvs nvs;
  const Snapshot previous = makeSnapshot('G', 7U, {"A", "B", "C"});
  const Snapshot candidate = makeSnapshot('H', 8U, {"A2", "B2", "C2"});
  nvs.seed(previous);
  nvs.seedBank(candidate);
  nvs.corruptPointer();
  const Snapshot afterReboot = nvs.rebootRead();
  assert(afterReboot.bank == previous.bank);
  assert(afterReboot.generation == previous.generation);
  assert(afterReboot.records == previous.records);
}

static void assertEmptySnapshotAndFailedEmptyPromotion() {
  const Snapshot previous = makeSnapshot('G', 7U, {"A"});
  const Snapshot empty = makeSnapshot('H', 8U, {});

  FakeNvs committed;
  committed.seed(previous);
  std::string error;
  assert(inktime::ackjournal::commitSnapshot(committed, previous.bank, empty, error));
  const Snapshot afterReboot = committed.rebootRead();
  assert(afterReboot.bank == empty.bank);
  assert(afterReboot.generation == empty.generation);
  assert(afterReboot.records.empty());

  FakeNvs failed;
  failed.seed(previous);
  failed.expected_count = 0U;
  failed.fault = FakeNvs::Fault::Pointer;
  assert(!inktime::ackjournal::commitSnapshot(failed, previous.bank, empty, error));
  const Snapshot retained = failed.rebootRead();
  assert(retained.bank == previous.bank);
  assert(retained.generation == previous.generation);
  assert(retained.records == previous.records);
}

static void assertCleanupFailureKeepsPromotedGenerationAuthoritative() {
  FakeNvs nvs;
  const Snapshot previous = makeSnapshot('G', 7U, {"A"});
  const Snapshot replacement = makeSnapshot('H', 8U, {"A2"});
  nvs.seed(previous);
  nvs.fault = FakeNvs::Fault::Cleanup;
  std::string error;
  assert(inktime::ackjournal::commitSnapshot(nvs, previous.bank, replacement, error));
  assert(nvs.cleanup_called);
  const Snapshot afterReboot = nvs.rebootRead();
  assert(afterReboot.bank == replacement.bank);
  assert(afterReboot.generation == replacement.generation);
  assert(afterReboot.records == replacement.records);
}

static void assertFullJournalFailurePreservesEveryOldRecord() {
  FakeNvs nvs;
  const Snapshot previous = makeBoundedSnapshot('G', 7U, 32U);
  const Snapshot replacement = makeBoundedSnapshot('H', 8U, 32U);
  nvs.seed(previous);
  nvs.expected_count = 32U;
  nvs.fault = FakeNvs::Fault::LastRecord;
  std::string error;
  assert(!inktime::ackjournal::commitSnapshot(nvs, previous.bank, replacement, error));
  const Snapshot afterReboot = nvs.rebootRead();
  assert(afterReboot.bank == previous.bank);
  assert(afterReboot.generation == previous.generation);
  assert(afterReboot.records == previous.records);
}

static void assertLegacyBatchAndDuplicateFailureWindows() {
  struct LegacyJournalModel {
    FakeNvs nvs;
    std::vector<std::string> legacy_records = {"legacy-0", "legacy-1", "legacy-2"};
    bool legacy_present = true;
    bool cleanup_failure = false;

    bool migrate(FakeNvs::Fault fault) {
      Snapshot replacement = makeSnapshot('H', 1U, {"legacy-0", "legacy-1", "legacy-2"});
      nvs.expected_count = 3U;
      nvs.fault = fault;
      std::string error;
      const bool canonical_committed =
        inktime::ackjournal::commitSnapshot(nvs, 0, replacement, error);
      if (inktime::ackjournal::legacyCleanupAllowed(canonical_committed)
          && !cleanup_failure) {
        legacy_present = false;
      }
      return canonical_committed;
    }

    std::vector<std::string> loadAfterReboot() const {
      const Snapshot canonical = nvs.rebootRead();
      return canonical.generation == 0U ? legacy_records : canonical.records;
    }
  };

  // A failed compact bN/count migration leaves the legacy source authoritative.
  LegacyJournalModel compact;
  assert(!compact.migrate(FakeNvs::Fault::FirstRecord));
  assert(compact.legacy_present);
  assert(compact.loadAfterReboot() == compact.legacy_records);

  // The same ordering must hold for the pre-blob per-field representation.
  LegacyJournalModel fields;
  assert(!fields.migrate(FakeNvs::Fault::Metadata));
  assert(fields.legacy_present);
  assert(fields.loadAfterReboot() == fields.legacy_records);

  // Canonical promotion precedes cleanup; a cleanup failure leaves both
  // generations readable and reboot still follows the promoted pointer.
  LegacyJournalModel migrated;
  migrated.cleanup_failure = true;
  assert(migrated.migrate(FakeNvs::Fault::None));
  assert(migrated.legacy_present);
  assert(migrated.loadAfterReboot() == std::vector<std::string>({
    "legacy-0", "legacy-1", "legacy-2"}));

  struct DashCfgModel {
    FakeNvs nvs;
    bool legacy_key_present = true;

    bool migrate(bool persist_failure) {
      const Snapshot replacement = makeSnapshot('H', 1U, {"dashcfg-ack"});
      nvs.expected_count = 1U;
      nvs.fault = persist_failure ? FakeNvs::Fault::Pointer : FakeNvs::Fault::None;
      std::string error;
      const bool canonical_committed =
        inktime::ackjournal::commitSnapshot(nvs, 0, replacement, error);
      if (inktime::ackjournal::legacyCleanupAllowed(canonical_committed)) {
        legacy_key_present = false;
      }
      return canonical_committed;
    }
  };
  DashCfgModel dashcfg;
  assert(!dashcfg.migrate(true));
  assert(dashcfg.legacy_key_present);
  assert(dashcfg.migrate(false));
  assert(!dashcfg.legacy_key_present);

  struct BatchPersistenceModel {
    std::vector<bool> outcomes;
    std::vector<std::string> durable_records;
    bool durable_claim = false;

    bool persistBatch(const std::vector<std::string>& records) {
      bool aggregate = true;
      for (size_t index = 0U; index < records.size(); ++index) {
        const bool result = outcomes[index];
        if (result) durable_records.push_back(records[index]);
        aggregate = inktime::ackjournal::allPersistenceSucceeded(aggregate, result);
      }
      durable_claim = aggregate;
      return aggregate;
    }
  };
  BatchPersistenceModel batch{{true, false, true}, {}, false};
  assert(!batch.persistBatch({"batch-0", "batch-1", "batch-2"}));
  assert(!batch.durable_claim);
  assert(batch.durable_records == std::vector<std::string>({"batch-0", "batch-2"}));

  struct AuthoritativeCallerModel {
    std::vector<std::string> durable_records = {"accepted", "permanent", "next"};
    std::vector<std::string> processed_records;
    bool cleanup_failure = true;
    bool retry_requested = false;
    bool duplicate_evidence_retained = false;

    bool remove(const std::string& id) {
      if (cleanup_failure && (id == "accepted" || id == "permanent")) return false;
      for (auto iterator = durable_records.begin(); iterator != durable_records.end(); ++iterator) {
        if (*iterator == id) {
          durable_records.erase(iterator);
          return true;
        }
      }
      return true;
    }

    void processAuthoritativeResult(const std::string& id, bool permanent) {
      const bool removed = remove(id);
      if (inktime::ackjournal::retainDuplicateEvidence(true, removed)) {
        duplicate_evidence_retained = true;
      }
      // An authoritative accepted/permanent result must not become a network
      // retry merely because local duplicate cleanup failed.
      (void)permanent;
      processed_records.push_back(id);
    }

    void processLaterWork(const std::string& id) {
      const bool removed = remove(id);
      assert(removed);
      processed_records.push_back(id);
    }
  };
  AuthoritativeCallerModel caller;
  caller.processAuthoritativeResult("accepted", false);
  caller.processAuthoritativeResult("permanent", true);
  caller.processLaterWork("next");
  assert(caller.duplicate_evidence_retained);
  assert(!caller.retry_requested);
  assert(caller.processed_records == std::vector<std::string>({
    "accepted", "permanent", "next"}));
  assert(caller.durable_records == std::vector<std::string>({
    "accepted", "permanent"}));
}

int main() {
  assertPreviousSnapshotAfterFault(FakeNvs::Fault::FirstRecord);
  assertPreviousSnapshotAfterFault(FakeNvs::Fault::MiddleRecord);
  assertPreviousSnapshotAfterFault(FakeNvs::Fault::LastRecord);
  assertPreviousSnapshotAfterFault(FakeNvs::Fault::RecordReadbackMismatch);
  assertPreviousSnapshotAfterFault(FakeNvs::Fault::Metadata);
  assertPreviousSnapshotAfterFault(FakeNvs::Fault::MetadataReadbackMismatch);
  assertPreviousSnapshotAfterFault(FakeNvs::Fault::Pointer);
  assertPreviousSnapshotAfterFault(FakeNvs::Fault::PointerTorn);
  assertPreviousSnapshotAfterFault(FakeNvs::Fault::Verify);
  assertInvalidPointerChoosesOlderCompleteGeneration();
  assertEmptySnapshotAndFailedEmptyPromotion();
  assertCleanupFailureKeepsPromotedGenerationAuthoritative();
  assertFullJournalFailurePreservesEveryOldRecord();
  assertLegacyBatchAndDuplicateFailureWindows();

  FakeNvs nvs;
  const Snapshot previous = makeSnapshot('G', 7U, {"A", "B", "C"});
  const Snapshot replacement = makeSnapshot('H', 8U, {"A2", "B2", "C2"});
  nvs.seed(previous);
  nvs.expected_count = 3U;
  std::string error;
  assert(inktime::ackjournal::commitSnapshot(nvs, previous.bank, replacement, error));
  const Snapshot committed = nvs.rebootRead();
  assert(committed.bank == replacement.bank);
  assert(committed.generation == replacement.generation);
  assert(committed.records == replacement.records);
  return 0;
}
