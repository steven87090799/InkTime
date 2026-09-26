#pragma once

#include <stdint.h>

namespace inktime {
namespace photopainter_sensor {

// Sensirion SHTC3 datasheet v4, section 5.2: power-up leaves the
// sensor idle; an ESP-only wake may find it already asleep. Wake just long
// enough to accept Sleep, without probing, reading ID or starting a conversion.
// SHTC3 shares VCC3V3 with the ESP/SD; no PMIC or GPIO power gating is used.
constexpr uint8_t kAddress = 0x70;
constexpr uint16_t kWakeCommand = 0x3517;
constexpr uint16_t kSleepCommand = 0xB098;
constexpr uint32_t kWakeWaitMs = 1U;  // Exceeds the 240 us wake-up time (Table 5 / Figure 7).

enum class SleepResult : uint8_t { CommandAccepted, WakeFailed, SleepUnconfirmed };

template <typename Bus, typename Wait>
SleepResult disableMeasurements(Bus& bus, Wait wait) {
  // One bounded attempt per command. Sleep can take effect even if its ACK
  // was lost; replaying it against an asleep sensor would provoke false bus
  // recovery. Never reset the shared bus just to disable an optional sensor.
  if (!bus.writeCommand(kAddress, kWakeCommand, false)) {
    return SleepResult::WakeFailed;
  }
  wait(kWakeWaitMs);
  return bus.writeCommand(kAddress, kSleepCommand, false)
      ? SleepResult::CommandAccepted : SleepResult::SleepUnconfirmed;
}

}  // namespace photopainter_sensor
}  // namespace inktime
