#pragma once

#include <stdint.h>

#include "hardware_profile.h"

namespace inktime {

enum class ChargeState : uint8_t {
  Unknown,
  Trickle,
  PreCharge,
  ConstantCurrent,
  ConstantVoltage,
  Done,
  NotCharging,
};

inline const char* chargeStateName(ChargeState state) {
  switch (state) {
    case ChargeState::Unknown: return "UNKNOWN";
    case ChargeState::Trickle: return "TRICKLE";
    case ChargeState::PreCharge: return "PRECHARGE";
    case ChargeState::ConstantCurrent: return "CONSTANT CURRENT";
    case ChargeState::ConstantVoltage: return "CONSTANT VOLTAGE";
    case ChargeState::Done: return "DONE";
    case ChargeState::NotCharging: return "NOT CHARGING";
  }
  return "UNKNOWN";
}

class PowerManager {
 public:
  virtual ~PowerManager() = default;
  virtual bool begin() = 0;
  virtual void refreshMeasurements() = 0;
  virtual PmicType type() const = 0;
  virtual PowerSourceState powerSourceState() const = 0;
  virtual bool isUsbConnected() const = 0;
  virtual bool batteryPresent() const = 0;
  virtual bool isCharging() const = 0;
  virtual ChargeState chargeState() const = 0;
  virtual float batteryVoltage() const = 0;
  virtual int batteryPercent() const = 0;
  virtual void prepareForDeepSleep() = 0;
};

inline const char* pmicTypeName(PmicType type) {
  switch (type) {
    case PmicType::None: return "none";
    case PmicType::AXP2101: return "axp2101";
    case PmicType::TG28: return "tg28";
    case PmicType::Unknown: return "unknown";
  }
  return "unknown";
}

}  // namespace inktime
