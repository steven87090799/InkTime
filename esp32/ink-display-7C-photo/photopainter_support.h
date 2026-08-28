#pragma once

#include "hardware_profile.h"

#if INKTIME_PHOTOPAINTER_ENABLED

#include <Arduino.h>
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
    uint8_t** output,
    const char* sourceSha256 = nullptr
  );
  bool loadFormalFrame(
    const char* sourceSha256,
    DisplayRotation rotation,
    uint8_t** output
  );
  bool convertFrame(
    const uint8_t* wire,
    size_t wireLength,
    bool indexed4,
    DisplayRotation rotation,
    uint8_t** output
  );
  bool convertAndCache(
    const uint8_t* wire,
    size_t wireLength,
    bool indexed4,
    uint32_t sourceHash,
    DisplayRotation rotation,
    uint8_t** output,
    const char* sourceSha256 = nullptr
  );
  bool writeFormalFrame(
    const char* sourceSha256,
    DisplayRotation rotation,
    const uint8_t* framebuffer,
    size_t length
  );
  bool runFormalFrameGc(
    const char* activeScheduleJson,
    const char* stagedNextScheduleJson,
    const char* currentFrameSha256,
    const char* lastGoodFrameSha256,
    const char* inFlightFrameSha256,
    const char* recoveryFrameSha256
  );
  bool writeActiveSchedule(const char* json, size_t length);
  bool readActiveSchedule(String& json);
  String activeScheduleId();
  bool writeStagedNextSchedule(const char* json, size_t length);
  bool readStagedNextSchedule(String& json);
  String stagedNextScheduleId();
  bool clearStagedNextSchedule();
  bool promoteStagedNextSchedule();
  bool displayFrame(const uint8_t* framebuffer, size_t length);
  bool displayPairingScreen(
    const char* ssid,
    const char* password,
    const char* setup_url,
    const char* pairing_code = nullptr,
    const char* footer = "VALID 5 MIN"
  );
  bool displayPowerStatusScreen();

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
  bool recoveryServiceRequested() const { return recoveryServiceRequested_; }
  bool wokeFromUserButton() const { return wokeFromUserButton_; }
  bool batteryStatusRequested() const { return batteryStatusRequested_; }
  PowerSourceState powerSourceState() const;
  bool usbConnected() const;
  PmicType pmicType() const;
  float batteryVoltage() const;
  int batteryPercent() const;
  float temperatureC() const { return temperatureC_; }
  float humidityPercent() const { return humidityPercent_; }
  bool environmentValid() const { return environmentValid_; }
  uint32_t lastRefreshDurationMs() const { return lastRefreshDurationMs_; }
  uint32_t sdReadBytes() const { return sdReadBytes_; }
  uint32_t sdWriteBytes() const { return sdWriteBytes_; }
  uint32_t sdWriteDurationMs() const { return sdWriteDurationMs_; }
  uint32_t i2cRetryCount() const;
  uint32_t i2cBusResetCount() const;
  uint32_t i2cFailClosedCount() const;
  uint32_t gcDeletedFiles() const { return gcDeletedFiles_; }
  uint32_t gcDeletedBytes() const { return gcDeletedBytes_; }
  uint32_t gcSkippedProtected() const { return gcSkippedProtected_; }
  const char* inFlightFormalFrameSha256() const {
    return formalFrameInFlightSha256_.c_str();
  }
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
  bool recoveryServiceRequested_ = false;
  bool wokeFromUserButton_ = false;
  bool batteryStatusRequested_ = false;
  bool earlyEpdTransportReady_ = false;
  bool earlyEpdPinsReady_ = false;
  bool environmentValid_ = false;
  float temperatureC_ = 0.0f;
  float humidityPercent_ = 0.0f;
  uint32_t lastRefreshDurationMs_ = 0;
  uint32_t sdReadBytes_ = 0;
  uint32_t sdWriteBytes_ = 0;
  uint32_t sdWriteDurationMs_ = 0;
  uint32_t gcDeletedFiles_ = 0;
  uint32_t gcDeletedBytes_ = 0;
  uint32_t gcSkippedProtected_ = 0;
  String formalFrameInFlightSha256_;
  CacheStatus cacheStatus_ = CacheStatus::Disabled;
  const char* lastError_ = "";
};

}  // namespace inktime

#endif
