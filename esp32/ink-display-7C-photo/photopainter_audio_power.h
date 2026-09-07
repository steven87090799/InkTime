#pragma once

#include <stddef.h>
#include <stdint.h>

namespace inktime {
namespace photopainter_audio {

// Rev2.0 schematic UP1 pin 16: ALDO3 -> Audio_VCC. ALDO2 is unconnected.
// Official power-test driver clears REG90 bit 2 for ALDO3. Do not change
// ALDO4 (EPD), DCDC1 (ESP/SD), charging, or PMIC sleep/IRQ configuration.
constexpr uint8_t kAddress = 0x34;
constexpr uint8_t kEnableRegister = 0x90;
constexpr uint8_t kAudioMask = 1U << 2U;

enum class Result : uint8_t { AlreadyOff, Disabled, ReadFailed, WriteFailed, VerifyFailed };

// Board startup only, after PA LOW and before any audio/I2S initialization.
// A write with uncertain completion is never replayed; a fresh boot may
// inspect the actual register state. Readback verifies all unrelated bits.
template <typename Bus>
Result powerDownUnusedAudio(Bus& bus) {
  uint8_t before = 0U;
  if (!bus.readRegister(kAddress, kEnableRegister, &before, 1U)) return Result::ReadFailed;
  if ((before & kAudioMask) == 0U) return Result::AlreadyOff;
  const uint8_t after = static_cast<uint8_t>(before & static_cast<uint8_t>(~kAudioMask));
  if (!bus.writeRegisters(kAddress, kEnableRegister, &after, 1U, false)) {
    return Result::WriteFailed;
  }
  uint8_t verified = 0U;
  if (!bus.readRegister(kAddress, kEnableRegister, &verified, 1U) || verified != after) {
    return Result::VerifyFailed;
  }
  return Result::Disabled;
}

}  // namespace photopainter_audio
}  // namespace inktime
