#ifndef INKTIME_MAX_AWAKE_RECOVERY_CORE_H
#define INKTIME_MAX_AWAKE_RECOVERY_CORE_H

#include <cstdint>

namespace inktime {

constexpr uint32_t kMaxAwakeRecoveryMagic = 0x494D4157U;  // "IMAW"
constexpr uint32_t kMaxAwakeRecoveryThreshold = 3U;
constexpr uint64_t kMaxAwakeSafeSleepSeconds = 60ULL * 60ULL;

struct MaxAwakeRecoveryState {
  uint32_t magic;
  uint32_t consecutiveTimeouts;
  uint32_t consecutiveTimeoutsInverse;
};

inline bool validMaxAwakeRecoveryState(const MaxAwakeRecoveryState& state) {
  return state.magic == kMaxAwakeRecoveryMagic
      && state.consecutiveTimeouts <= kMaxAwakeRecoveryThreshold
      && state.consecutiveTimeoutsInverse == ~state.consecutiveTimeouts;
}

inline uint32_t maxAwakeRecoveryCount(const MaxAwakeRecoveryState& state) {
  return validMaxAwakeRecoveryState(state) ? state.consecutiveTimeouts : 0U;
}

inline void resetMaxAwakeRecoveryState(MaxAwakeRecoveryState& state) {
  state.consecutiveTimeouts = 0U;
  state.consecutiveTimeoutsInverse = ~0U;
  state.magic = kMaxAwakeRecoveryMagic;
}

inline uint32_t recordMaxAwakeTimeout(MaxAwakeRecoveryState& state) {
  const uint32_t current = maxAwakeRecoveryCount(state);
  const uint32_t next = current < kMaxAwakeRecoveryThreshold ? current + 1U : current;
  state.consecutiveTimeouts = next;
  state.consecutiveTimeoutsInverse = ~next;
  state.magic = kMaxAwakeRecoveryMagic;
  return next;
}

inline bool shouldEnterMaxAwakeSafeSleep(const MaxAwakeRecoveryState& state) {
  return maxAwakeRecoveryCount(state) >= kMaxAwakeRecoveryThreshold;
}

}  // namespace inktime

#endif
