#pragma once

#include "hardware_profile.h"

#if INKTIME_PHOTOPAINTER_ENABLED

#include <stddef.h>
#include <stdint.h>
#include <time.h>

namespace inktime {

enum class CacheStatus : uint8_t {
  Disabled,
  Miss,
  Hit,
  Written,
  Invalid,
  Error,
};

const char* cacheStatusName(CacheStatus status);

class PhotoPainterSupport {
 public:
  explicit PhotoPainterSupport(const BoardConfig& board);
  ~PhotoPainterSupport();

  bool begin();
  uint8_t* allocateWireBuffer(size_t length) const;
  bool loadCachedFrame(
    uint32_t sourceHash,
    DisplayRotation rotation,
    uint8_t** output
  );
  bool convertAndCache(
    const uint8_t* wire,
    size_t wireLength,
    bool indexed4,
    uint32_t sourceHash,
    DisplayRotation rotation,
    uint8_t** output
  );
  bool displayFrame(const uint8_t* framebuffer, size_t length);

  bool writeRtc(time_t epoch);
  bool readRtc(time_t& epoch);
  void refreshPowerState();
  void readEnvironment();
  void prepareForDeepSleep();
  void enableWakeSources();

  bool psramReady() const { return psramReady_; }
  bool flashReady() const { return flashReady_; }
  bool hardwareReady() const { return hardwareReady_; }
  bool sdReady() const { return sdReady_; }
  bool rtcReady() const { return rtcReady_; }
  bool shtc3Ready() const { return shtc3Ready_; }
  bool forceNetworkRefresh() const { return forceNetworkRefresh_; }
  bool wokeFromUserButton() const { return wokeFromUserButton_; }
  bool usbConnected() const;
  bool powerSourceKnown() const;
  PmicType pmicType() const;
  float batteryVoltage() const;
  int batteryPercent() const;
  float temperatureC() const { return temperatureC_; }
  float humidityPercent() const { return humidityPercent_; }
  bool environmentValid() const { return environmentValid_; }
  uint32_t lastRefreshDurationMs() const { return lastRefreshDurationMs_; }
  CacheStatus cacheStatus() const { return cacheStatus_; }
  const char* lastError() const { return lastError_; }

 private:
  struct Impl;
  const BoardConfig& board_;
  Impl* impl_ = nullptr;
  bool psramReady_ = false;
  bool flashReady_ = false;
  bool hardwareReady_ = false;
  bool sdReady_ = false;
  bool rtcReady_ = false;
  bool shtc3Ready_ = false;
  bool forceNetworkRefresh_ = false;
  bool wokeFromUserButton_ = false;
  bool environmentValid_ = false;
  float temperatureC_ = 0.0f;
  float humidityPercent_ = 0.0f;
  uint32_t lastRefreshDurationMs_ = 0;
  CacheStatus cacheStatus_ = CacheStatus::Disabled;
  const char* lastError_ = "";
};

}  // namespace inktime

#endif
