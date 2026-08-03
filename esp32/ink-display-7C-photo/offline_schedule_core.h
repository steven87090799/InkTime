#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace inktime {

// The compact firmware configuration is intentionally bounded.  The server
// may retain more history, but a device only stages twelve formal slots.
constexpr uint8_t kMaxOfflineSlots = 12;
constexpr uint8_t kMaxAckJournalEntries = 32;
constexpr int32_t kOfflineScheduleSchemaVersion = 1;

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

struct OfflineScheduleContract {
  int32_t schemaVersion;
  const char* deliveryMode;
  const char* targetDate;
  const char* localDate;
  const char* timezone;
  int64_t targetStartEpoch;
  int64_t targetEndEpoch;
  int64_t nowEpoch;
  uint32_t configVersion;
  uint32_t currentConfigVersion;
  int32_t rotation;
  const char* panelProfile;
  const char* expectedPanelProfile;
  const char* buttonWakeAction;
  uint8_t slotCount;
  uint8_t scheduleCount;
  bool queueIdentityValid;
  bool sha256Valid;
  bool slotEpochsValid;
};

inline bool validIsoLocalDate(const char* value) {
  if (value == nullptr || strlen(value) != 10U) return false;
  for (uint8_t index = 0; index < 10U; ++index) {
    if (index == 4U || index == 7U) {
      if (value[index] != '-') return false;
    } else if (value[index] < '0' || value[index] > '9') {
      return false;
    }
  }
  const int month = (value[5] - '0') * 10 + (value[6] - '0');
  const int day = (value[8] - '0') * 10 + (value[9] - '0');
  return month >= 1 && month <= 12 && day >= 1 && day <= 31;
}

inline bool validOfflineScheduleContract(const OfflineScheduleContract& contract) {
  if (contract.schemaVersion != kOfflineScheduleSchemaVersion
      || contract.deliveryMode == nullptr
      || strcmp(contract.deliveryMode, "inktime_offline_schedule") != 0
      || !validIsoLocalDate(contract.targetDate)
      || !validIsoLocalDate(contract.localDate)
      || strcmp(contract.targetDate, contract.localDate) != 0
      || contract.timezone == nullptr || contract.timezone[0] == '\0'
      || strlen(contract.timezone) > 64U
      || contract.targetStartEpoch <= 0
      || contract.targetEndEpoch <= contract.targetStartEpoch
      || contract.nowEpoch < contract.targetStartEpoch
      || contract.nowEpoch >= contract.targetEndEpoch
      || contract.configVersion < contract.currentConfigVersion
      || (contract.rotation != 0 && contract.rotation != 180)
      || contract.panelProfile == nullptr || contract.panelProfile[0] == '\0'
      || contract.expectedPanelProfile == nullptr
      || (strcmp(contract.panelProfile, "safe_4c") != 0
          && strcmp(contract.panelProfile, contract.expectedPanelProfile) != 0)
      || contract.buttonWakeAction == nullptr
      || (strcmp(contract.buttonWakeAction, "check_new") != 0
          && strcmp(contract.buttonWakeAction, "local_next") != 0)
      || contract.slotCount == 0U || contract.slotCount > kMaxOfflineSlots
      || contract.slotCount != contract.scheduleCount
      || !contract.queueIdentityValid || !contract.sha256Valid || !contract.slotEpochsValid) {
    return false;
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

constexpr uint64_t kOfflineRetryFirstSeconds = 15ULL * 60ULL;
constexpr uint64_t kOfflineRetrySecondSeconds = 30ULL * 60ULL;
constexpr uint64_t kOfflineRetryMaximumSeconds = 60ULL * 60ULL;
constexpr uint64_t kOfflineRetryMaximumHorizonSeconds = 7ULL * 24ULL * 60ULL * 60ULL;

inline uint64_t offlineRetryFallbackSeconds(uint8_t attempt) {
  if (attempt == 0U) return kOfflineRetryFirstSeconds;
  if (attempt == 1U) return kOfflineRetrySecondSeconds;
  return kOfflineRetryMaximumSeconds;
}

inline bool validOfflineRetryEpoch(uint64_t nowEpoch, int64_t retryEpoch) {
  if (retryEpoch <= static_cast<int64_t>(nowEpoch)) return false;
  return static_cast<uint64_t>(retryEpoch) - nowEpoch <= kOfflineRetryMaximumHorizonSeconds;
}

struct OfflineRetryPlan {
  uint64_t sleepUntilEpoch;
  uint8_t nextAttempt;
  bool serverProvided;
};

inline OfflineRetryPlan buildOfflineRetryPlan(
  uint64_t nowEpoch,
  uint8_t attempt,
  int64_t serverRetryEpoch
) {
  if (validOfflineRetryEpoch(nowEpoch, serverRetryEpoch)) {
    return {
      static_cast<uint64_t>(serverRetryEpoch),
      0U,
      true,
    };
  }
  const uint8_t boundedAttempt = attempt > 2U ? 2U : attempt;
  const uint8_t nextAttempt = boundedAttempt >= 2U ? 2U : boundedAttempt + 1U;
  return {
    nowEpoch + offlineRetryFallbackSeconds(boundedAttempt),
    nextAttempt,
    false,
  };
}

struct OfflineDisplayIntent {
  bool manualPreview;
  bool ownsFormalSlot;
  bool emitsTerminalAck;
};

inline OfflineDisplayIntent offlineDisplayIntent(bool localNext) {
  return {localNext, !localNext, !localNext};
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
