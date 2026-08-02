#pragma once

#include <stdint.h>

namespace inktime {

// The compact firmware configuration is intentionally bounded.  The server
// may retain more history, but a device only stages twelve formal slots.
constexpr uint8_t kMaxOfflineSlots = 12;
constexpr uint8_t kMaxAckJournalEntries = 32;

struct OfflineSlot {
  uint8_t hour;
  uint8_t minute;
};

inline bool validOfflineSlot(const OfflineSlot& slot) {
  return slot.hour < 24U && slot.minute < 60U;
}

inline bool validateOfflineSlots(const OfflineSlot* slots, uint8_t count) {
  if (slots == nullptr || count == 0 || count > kMaxOfflineSlots) return false;
  for (uint8_t index = 0; index < count; ++index) {
    if (!validOfflineSlot(slots[index])) return false;
    if (index > 0) {
      const uint16_t previous = static_cast<uint16_t>(slots[index - 1].hour) * 60U + slots[index - 1].minute;
      const uint16_t current = static_cast<uint16_t>(slots[index].hour) * 60U + slots[index].minute;
      if (current <= previous) return false;  // sorted and unique
    }
  }
  return true;
}

struct OfflineWakePlan {
  uint64_t prefetchEpoch;
  uint64_t displayEpoch;
  uint64_t sleepUntilEpoch;
};

inline bool buildOfflineWakePlan(
  uint64_t nowEpoch,
  uint64_t prefetchEpoch,
  uint64_t displayEpoch,
  OfflineWakePlan& output
) {
  if (displayEpoch <= nowEpoch || prefetchEpoch >= displayEpoch) return false;
  output.prefetchEpoch = prefetchEpoch;
  output.displayEpoch = displayEpoch;
  output.sleepUntilEpoch = prefetchEpoch;
  return true;
}

inline uint64_t exactSleepSeconds(uint64_t nowEpoch, uint64_t nextEpoch) {
  return nextEpoch > nowEpoch ? nextEpoch - nowEpoch : 1U;
}

class AckJournal {
 public:
  bool contains(const char* key) const {
    if (key == nullptr || key[0] == '\0') return false;
    for (uint8_t index = 0; index < count_; ++index) {
      if (equals(keys_[index], key)) return true;
    }
    return false;
  }

  bool remember(const char* key) {
    if (key == nullptr || key[0] == '\0') return false;
    if (contains(key)) return true;
    if (count_ == kMaxAckJournalEntries) {
      for (uint8_t index = 1; index < count_; ++index) copy(keys_[index - 1], keys_[index]);
      --count_;
    }
    copy(keys_[count_], key);
    ++count_;
    return true;
  }

  uint8_t size() const { return count_; }

 private:
  static constexpr uint8_t kKeyBytes = 96;
  char keys_[kMaxAckJournalEntries][kKeyBytes] = {};
  uint8_t count_ = 0;

  static bool equals(const char* left, const char* right) {
    for (uint8_t index = 0; index < kKeyBytes; ++index) {
      if (left[index] != right[index]) return false;
      if (left[index] == '\0') return true;
    }
    return true;
  }

  static void copy(char* destination, const char* source) {
    uint8_t index = 0;
    for (; index + 1U < kKeyBytes && source[index] != '\0'; ++index) destination[index] = source[index];
    destination[index] = '\0';
    for (++index; index < kKeyBytes; ++index) destination[index] = '\0';
  }
};

}  // namespace inktime
