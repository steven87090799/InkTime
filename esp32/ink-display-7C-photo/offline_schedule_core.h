#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace inktime {

// The compact firmware configuration is intentionally bounded.  The server
// may retain more history, but a device only stages twenty-four formal slots.
constexpr uint8_t kMaxOfflineSlots = 24;
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
  const int year = (value[0] - '0') * 1000 + (value[1] - '0') * 100
                 + (value[2] - '0') * 10 + (value[3] - '0');
  const int month = (value[5] - '0') * 10 + (value[6] - '0');
  const int day = (value[8] - '0') * 10 + (value[9] - '0');
  if (year < 2000 || month < 1 || month > 12 || day < 1) return false;
  const bool leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
  const uint8_t days[] = {
    0, 31, static_cast<uint8_t>(leap ? 29 : 28), 31, 30, 31, 30,
    31, 31, 30, 31, 30, 31,
  };
  return day <= days[month];
}

inline bool nextIsoLocalDateValue(const char* value, char* output, size_t outputSize) {
  if (!validIsoLocalDate(value) || output == nullptr || outputSize < 11U) return false;
  int year = (value[0] - '0') * 1000 + (value[1] - '0') * 100
           + (value[2] - '0') * 10 + (value[3] - '0');
  int month = (value[5] - '0') * 10 + (value[6] - '0');
  int day = (value[8] - '0') * 10 + (value[9] - '0');
  const bool leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
  const uint8_t days[] = {
    0, 31, static_cast<uint8_t>(leap ? 29 : 28), 31, 30, 31, 30,
    31, 31, 30, 31, 30, 31,
  };
  ++day;
  if (day > days[month]) {
    day = 1;
    ++month;
    if (month > 12) {
      if (year == 9999) return false;
      month = 1;
      ++year;
    }
  }
  output[0] = static_cast<char>('0' + year / 1000);
  output[1] = static_cast<char>('0' + (year / 100) % 10);
  output[2] = static_cast<char>('0' + (year / 10) % 10);
  output[3] = static_cast<char>('0' + year % 10);
  output[4] = '-';
  output[5] = static_cast<char>('0' + month / 10);
  output[6] = static_cast<char>('0' + month % 10);
  output[7] = '-';
  output[8] = static_cast<char>('0' + day / 10);
  output[9] = static_cast<char>('0' + day % 10);
  output[10] = '\0';
  return true;
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

// A next-day snapshot is validated against the currently active schedule by
// the firmware before promotion.  It deliberately has a separate contract so
// the existing current-day aggregate initializer remains source-compatible.
struct OfflineNextScheduleContract {
  int32_t schemaVersion;
  const char* deliveryMode;
  const char* targetDate;
  const char* timezone;
  int64_t targetStartEpoch;
  int64_t targetEndEpoch;
  int64_t activeTargetEndEpoch;
  int64_t nowEpoch;
  uint32_t configVersion;
  uint32_t currentConfigVersion;
  int32_t rotation;
  const char* panelProfile;
  const char* expectedPanelProfile;
  const char* buttonWakeAction;
  uint8_t slotCount;
  uint8_t scheduleCount;
  bool targetDateIsNext;
  bool queueIdentityValid;
  bool sha256Valid;
  bool slotEpochsValid;
};

inline bool validOfflineNextScheduleContract(const OfflineNextScheduleContract& contract) {
  if (contract.schemaVersion != kOfflineScheduleSchemaVersion
      || contract.deliveryMode == nullptr
      || strcmp(contract.deliveryMode, "inktime_offline_schedule") != 0
      || !validIsoLocalDate(contract.targetDate)
      || contract.timezone == nullptr || contract.timezone[0] == '\0'
      || strlen(contract.timezone) > 64U
      || contract.targetStartEpoch <= 0
      || contract.targetEndEpoch <= contract.targetStartEpoch
      || contract.activeTargetEndEpoch != contract.targetStartEpoch
      || contract.nowEpoch <= 0
      || contract.nowEpoch >= contract.targetStartEpoch
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
      || !contract.targetDateIsNext
      || !contract.queueIdentityValid || !contract.sha256Valid || !contract.slotEpochsValid) {
    return false;
  }
  return true;
}

inline bool scheduleHasDueFormalSlot(
  const int64_t* showAtEpochs,
  uint8_t count,
  uint64_t nowEpoch,
  uint64_t graceSeconds = 15ULL * 60ULL
) {
  if (showAtEpochs == nullptr || count == 0U || count > kMaxOfflineSlots) return false;
  for (uint8_t index = 0; index < count; ++index) {
    if (showAtEpochs[index] <= 0 || showAtEpochs[index] > static_cast<int64_t>(nowEpoch)) continue;
    if (nowEpoch - static_cast<uint64_t>(showAtEpochs[index]) <= graceSeconds) return true;
  }
  return false;
}

inline bool validOfflineNextPrefetchEpoch(
  int64_t nowEpoch,
  int64_t targetStartEpoch,
  int64_t firstSlotEpoch,
  uint16_t leadMinutes,
  int64_t& outputEpoch
) {
  outputEpoch = 0;
  if (nowEpoch <= 0 || targetStartEpoch <= nowEpoch || firstSlotEpoch <= targetStartEpoch
      || leadMinutes > 120U) return false;
  const int64_t candidate = firstSlotEpoch - static_cast<int64_t>(leadMinutes) * 60LL;
  if (candidate <= nowEpoch || candidate >= firstSlotEpoch) return false;
  outputEpoch = candidate;
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

inline bool validOfflineRetryEpoch(
  uint64_t nowEpoch,
  int64_t retryEpoch,
  int64_t nextSlotEpoch = 0
) {
  if (nextSlotEpoch < 0) return false;
  if (retryEpoch <= static_cast<int64_t>(nowEpoch)) return false;
  if (static_cast<uint64_t>(retryEpoch) - nowEpoch > kOfflineRetryMaximumHorizonSeconds) {
    return false;
  }
  if (nextSlotEpoch > 0
      && (nextSlotEpoch <= static_cast<int64_t>(nowEpoch) || retryEpoch >= nextSlotEpoch)) {
    return false;
  }
  return true;
}

struct OfflineRetryPlan {
  uint64_t sleepUntilEpoch;
  uint8_t nextAttempt;
  bool serverProvided;
};

inline OfflineRetryPlan buildOfflineRetryPlan(
  uint64_t nowEpoch,
  uint8_t attempt,
  int64_t serverRetryEpoch,
  int64_t nextSlotEpoch = 0
) {
  if (validOfflineRetryEpoch(nowEpoch, serverRetryEpoch, nextSlotEpoch)) {
    return {
      static_cast<uint64_t>(serverRetryEpoch),
      0U,
      true,
    };
  }
  const uint8_t boundedAttempt = attempt > 2U ? 2U : attempt;
  const uint8_t nextAttempt = boundedAttempt >= 2U ? 2U : boundedAttempt + 1U;
  uint64_t fallback = nowEpoch + offlineRetryFallbackSeconds(boundedAttempt);
  if (nextSlotEpoch > static_cast<int64_t>(nowEpoch)
      && fallback >= static_cast<uint64_t>(nextSlotEpoch)) {
    const uint64_t safeDeadline = static_cast<uint64_t>(nextSlotEpoch) > 60U
      ? static_cast<uint64_t>(nextSlotEpoch) - 60U
      : 0U;
    if (safeDeadline > nowEpoch && safeDeadline < static_cast<uint64_t>(nextSlotEpoch)) {
      fallback = safeDeadline;
    } else if (static_cast<uint64_t>(nextSlotEpoch) > nowEpoch + 1U) {
      // There is no way to satisfy both a 60-second minimum and a strictly
      // earlier imminent Slot; preserving the no-crossing guard wins.
      fallback = static_cast<uint64_t>(nextSlotEpoch) - 1U;
    }
  }
  return {
    fallback,
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

inline int16_t nextOfflinePreviewSlot(
  const int64_t* showAtEpochs,
  const char* const* sha256Values,
  uint8_t count,
  int16_t cursorIndex,
  uint64_t nowEpoch,
  const char* currentSha256
) {
  if (showAtEpochs == nullptr || sha256Values == nullptr || count == 0U
      || count > kMaxOfflineSlots) return -1;
  const bool firstPress = cursorIndex < 0 || cursorIndex >= static_cast<int16_t>(count);
  int16_t start = -1;
  if (firstPress) {
    for (uint8_t index = 0; index < count; ++index) {
      if (showAtEpochs[index] > static_cast<int64_t>(nowEpoch)) {
        start = static_cast<int16_t>(index);
        break;
      }
    }
    if (start < 0) return -1;
  } else {
    start = static_cast<int16_t>((cursorIndex + 1) % count);
  }
  for (uint8_t offset = 0; offset < count; ++offset) {
    const int16_t index = static_cast<int16_t>((start + offset) % count);
    if (firstPress && showAtEpochs[index] <= static_cast<int64_t>(nowEpoch)) continue;
    if (sha256Values[index] == nullptr || sha256Values[index][0] == '\0') continue;
    if (currentSha256 != nullptr && currentSha256[0] != '\0'
        && strcmp(sha256Values[index], currentSha256) == 0) continue;
    return index;
  }
  return -1;
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
