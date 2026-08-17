#pragma once

#include <stdint.h>

namespace inktime {

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
