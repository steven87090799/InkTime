#include <cassert>

#include "ack_journal_storage_budget.h"

int main() {
  static_assert(
      inktime::ackjournal::kMaximumEntries == inktime::kMaxAckJournalEntries);
  static_assert(
      inktime::ackjournal::kWorstCaseNvsBytes
      <= inktime::ackjournal::kTargetNvsPartitionBytes);
  static_assert(inktime::ackjournal::kAckJournalActivePointerBytes == 20U);
  assert(inktime::ackjournal::kTargetNvsPartitionBytes == 0x80000U);
  assert(inktime::ackjournal::kAckJournalBlobBytes > 300U);
  assert(inktime::ackjournal::kAckJournalPeakRecordBytes
         < inktime::ackjournal::kTargetNvsPartitionBytes);
  assert(inktime::ackjournal::kWorstCaseNvsBytes
         <= inktime::ackjournal::kTargetNvsPartitionBytes);
  return 0;
}
