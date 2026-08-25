#ifndef INKTIME_MAX_AWAKE_RECOVERY_CORE_H
#define INKTIME_MAX_AWAKE_RECOVERY_CORE_H

#include <cstdint>

namespace inktime {

constexpr uint32_t kMaxAwakeRecoveryMagic = 0x494D4132U;  // "IMA2"
constexpr uint32_t kMaxAwakeRecoveryThreshold = 3U;
constexpr uint32_t kMaxAwakeRecoveryMaximum = 5U;
constexpr uint64_t kMaxAwakeSafeSleepFirstSeconds = 60ULL * 60ULL;
constexpr uint64_t kMaxAwakeSafeSleepSecondSeconds = 6ULL * 60ULL * 60ULL;
constexpr uint64_t kMaxAwakeSafeSleepDailySeconds = 24ULL * 60ULL * 60ULL;

struct MaxAwakeRecoveryState {
  uint32_t magic;
  uint32_t consecutiveTimeouts;
  uint32_t consecutiveTimeoutsInverse;
  uint32_t backoffCompletedForCount;
  uint32_t backoffCompletedForCountInverse;
};

inline bool validMaxAwakeRecoveryState(const MaxAwakeRecoveryState& state) {
  return state.magic == kMaxAwakeRecoveryMagic
      && state.consecutiveTimeouts <= kMaxAwakeRecoveryMaximum
      && state.consecutiveTimeoutsInverse == ~state.consecutiveTimeouts
      && state.backoffCompletedForCount <= state.consecutiveTimeouts
      && state.backoffCompletedForCountInverse == ~state.backoffCompletedForCount;
}

inline uint32_t maxAwakeRecoveryCount(const MaxAwakeRecoveryState& state) {
  if (validMaxAwakeRecoveryState(state)) return state.consecutiveTimeouts;
  // An uninitialised RTC_NOINIT region is a normal fresh boot.  Once our magic
  // exists, however, a damaged inverse/count must fail closed to daily backoff.
  return state.magic == kMaxAwakeRecoveryMagic ? kMaxAwakeRecoveryMaximum : 0U;
}

inline void resetMaxAwakeRecoveryState(MaxAwakeRecoveryState& state) {
  state.consecutiveTimeouts = 0U;
  state.consecutiveTimeoutsInverse = ~0U;
  state.backoffCompletedForCount = 0U;
  state.backoffCompletedForCountInverse = ~0U;
  state.magic = kMaxAwakeRecoveryMagic;
}

inline void writeMaxAwakeRecoveryState(
    MaxAwakeRecoveryState& state,
    uint32_t count,
    uint32_t completedForCount) {
  state.consecutiveTimeouts = count;
  state.consecutiveTimeoutsInverse = ~count;
  state.backoffCompletedForCount = completedForCount;
  state.backoffCompletedForCountInverse = ~completedForCount;
  state.magic = kMaxAwakeRecoveryMagic;
}

inline uint32_t recordMaxAwakeTimeout(MaxAwakeRecoveryState& state) {
  const uint32_t current = maxAwakeRecoveryCount(state);
  const uint32_t next = current < kMaxAwakeRecoveryMaximum ? current + 1U : current;
  // Even at the saturated daily stage, every failed probation must require a
  // fresh backoff.  Keeping completed one step behind encodes that obligation.
  writeMaxAwakeRecoveryState(state, next, next > 0U ? next - 1U : 0U);
  return next;
}

inline uint32_t recordMaxAwakeSupervisorFailure(MaxAwakeRecoveryState& state) {
  const uint32_t current = maxAwakeRecoveryCount(state);
  const uint32_t next = current < kMaxAwakeRecoveryThreshold
      ? kMaxAwakeRecoveryThreshold
      : (current < kMaxAwakeRecoveryMaximum ? current + 1U : current);
  writeMaxAwakeRecoveryState(state, next, next - 1U);
  return next;
}

inline bool shouldEnterMaxAwakeSafeSleep(const MaxAwakeRecoveryState& state) {
  if (!validMaxAwakeRecoveryState(state)) {
    return state.magic == kMaxAwakeRecoveryMagic;
  }
  return state.consecutiveTimeouts >= kMaxAwakeRecoveryThreshold
      && state.backoffCompletedForCount < state.consecutiveTimeouts;
}

inline uint64_t maxAwakeSafeSleepSeconds(const MaxAwakeRecoveryState& state) {
  const uint32_t count = maxAwakeRecoveryCount(state);
  if (count <= kMaxAwakeRecoveryThreshold) return kMaxAwakeSafeSleepFirstSeconds;
  if (count == kMaxAwakeRecoveryThreshold + 1U) return kMaxAwakeSafeSleepSecondSeconds;
  return kMaxAwakeSafeSleepDailySeconds;
}

inline void markMaxAwakeSafeSleepCompleted(MaxAwakeRecoveryState& state) {
  const uint32_t count = maxAwakeRecoveryCount(state);
  const uint32_t safeCount = count >= kMaxAwakeRecoveryThreshold
      ? count
      : kMaxAwakeRecoveryThreshold;
  writeMaxAwakeRecoveryState(state, safeCount, safeCount);
}

}  // namespace inktime

#endif
