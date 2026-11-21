#pragma once

#include <stdint.h>

namespace inktime {

constexpr int64_t kLegacyWakeLimitSeconds = 24LL * 60LL * 60LL;
constexpr int64_t kLegacyWakeRemainderSeconds = 2LL * 60LL * 60LL;

inline bool validLegacyRefreshEpoch(int64_t now, int64_t target, int64_t maxDelay) {
  return now > 0 && target > now && target - now <= maxDelay;
}

constexpr uint32_t kForceNetworkRefreshHoldMs = 1200U;
constexpr uint32_t kRecoveryServiceHoldMs = 4000U;
constexpr uint32_t kUserButtonHoldMeasurementLimitMs = 5000U;
constexpr uint32_t kUserButtonDoubleClickWindowMs = 450U;
constexpr uint32_t kUserButtonDebounceMs = 35U;

static_assert(kRecoveryServiceHoldMs < kUserButtonHoldMeasurementLimitMs,
              "PhotoPainter recovery hold must be reachable");

constexpr bool shouldForceNetworkRefresh(uint32_t heldMs) {
  return heldMs >= kForceNetworkRefreshHoldMs;
}

constexpr bool shouldRequestRecoveryService(uint32_t heldMs) {
  return heldMs >= kRecoveryServiceHoldMs;
}

constexpr uint64_t gpioWakeMask(int8_t gpio) {
  return gpio >= 0 && gpio < 64
      ? (1ULL << static_cast<uint8_t>(gpio))
      : 0ULL;
}

constexpr bool ext1WakeStatusContainsUserButton(uint64_t wakeStatus, int8_t userGpio) {
  const uint64_t userMask = gpioWakeMask(userGpio);
  return userMask != 0ULL && (wakeStatus & userMask) != 0ULL;
}

}  // namespace inktime
