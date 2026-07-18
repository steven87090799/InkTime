#pragma once

#include "hardware_profile.h"

#if INKTIME_PHOTOPAINTER_ENABLED

#include <Arduino.h>
#include <SPI.h>

namespace inktime {

class Spectra6_73 {
 public:
  Spectra6_73(SPIClass& spi, const BoardConfig& board);

  bool begin();
  bool displayFrame(const uint8_t* framebuffer, size_t length);
  void safeShutdown();
  uint32_t lastRefreshDurationMs() const { return lastRefreshDurationMs_; }
  const char* lastError() const { return lastError_; }

 private:
  bool waitUntilReady(uint32_t timeoutMs = 60000);
  void hardwareReset();
  void sendCommand(uint8_t command);
  void sendData(uint8_t data);
  void sendData(const uint8_t* data, size_t length);
  bool powerOff();
  void deepSleep();

  SPIClass& spi_;
  const BoardConfig& board_;
  bool sessionActive_ = false;
  bool initialized_ = false;
  uint32_t lastRefreshDurationMs_ = 0;
  const char* lastError_ = "";
};

}  // namespace inktime

#endif
