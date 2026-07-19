#pragma once

#include <stdint.h>

#include "hardware_profile.h"

namespace inktime {

class PowerManager {
 public:
  virtual ~PowerManager() = default;
  virtual bool begin() = 0;
  virtual void refreshMeasurements() = 0;
  virtual PmicType type() const = 0;
  virtual bool isUsbConnected() const = 0;
  virtual bool isPowerSourceKnown() const = 0;
  virtual float batteryVoltage() const = 0;
  virtual int batteryPercent() const = 0;
  virtual bool allowDisplayRefresh(uint16_t minimumMillivolts) const = 0;
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
