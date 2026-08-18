#pragma once

#include <stdint.h>

namespace inktime {

constexpr uint32_t kForceNetworkRefreshHoldMs = 1200U;
constexpr uint32_t kRecoveryServiceHoldMs = 4000U;
constexpr uint32_t kUserButtonHoldMeasurementLimitMs = 5000U;

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
