#pragma once

#include <stdint.h>

namespace inktime {

struct NetworkBudget {
  uint32_t fastConnectMs;
  uint32_t fallbackConnectMs;
  uint32_t totalConnectMs;
  uint32_t ntpMs;
  uint32_t manifestMs;
  uint32_t payloadIdleMs;
  uint32_t payloadTotalMs;
  uint32_t statusMs;
};

constexpr NetworkBudget networkBudget(bool usbPower) {
  return usbPower
    ? NetworkBudget{5000, 15000, 20000, 10000, 30000, 10000, 90000, 15000}
    : NetworkBudget{4000, 8000, 12000, 6000, 15000, 5000, 60000, 10000};
}

enum class ConnectAction : uint8_t { Fast, Fallback, Sleep };

inline ConnectAction nextConnectAction(bool hintValid, bool fastAttempted, bool fallbackAttempted,
                                       uint32_t elapsedMs, bool usbPower) {
  const NetworkBudget budget = networkBudget(usbPower);
  if (elapsedMs >= budget.totalConnectMs) return ConnectAction::Sleep;
  if (hintValid && !fastAttempted) return ConnectAction::Fast;
  if (!fallbackAttempted) return ConnectAction::Fallback;
  return ConnectAction::Sleep;
}

struct PanelCapabilities {
  bool supportsPartialRefresh;
  bool requiresFullRefresh;
  bool supportsHibernate;
  uint32_t minimumRefreshIntervalMs;
};

constexpr PanelCapabilities kSpectraCapabilities = {false, true, true, 300000};

inline bool shouldRefresh(bool forced, bool payloadMatches, uint32_t elapsedSinceRefreshMs,
                          const PanelCapabilities& capabilities = kSpectraCapabilities) {
  if (forced) return true;
  if (payloadMatches) return false;
  return elapsedSinceRefreshMs >= capabilities.minimumRefreshIntervalMs;
}

inline bool connectionHintChanged(bool valid, uint8_t oldChannel, uint8_t newChannel,
                                  const uint8_t oldBssid[6], const uint8_t newBssid[6]) {
  if (!valid || oldChannel != newChannel) return true;
  for (uint8_t index = 0; index < 6; ++index) {
    if (oldBssid[index] != newBssid[index]) return true;
  }
  return false;
}

}  // namespace inktime
