#pragma once

#include "hardware_profile.h"

#if INKTIME_PHOTOPAINTER_ENABLED

#include <Arduino.h>
#include <driver/spi_master.h>

namespace inktime {

bool prepareSpectra6ColdBootTransport(const BoardConfig& board);

class Spectra6_73 {
 public:
  explicit Spectra6_73(const BoardConfig& board);

  bool begin();
  bool displayFrame(const uint8_t* framebuffer, size_t length);
  void safeShutdown();
  uint32_t lastRefreshDurationMs() const { return lastRefreshDurationMs_; }
  const char* lastError() const { return lastError_; }

 private:
  bool waitForBusyAssertion(uint32_t timeoutMs = 2000);
  bool waitUntilReady(uint32_t timeoutMs = 60000);
  bool waitForBusyCycle();
  bool transmit(const uint8_t* data, size_t length);
  void hardwareReset();
  void sendCommand(uint8_t command);
  void sendData(uint8_t data);
  void sendData(const uint8_t* data, size_t length);
  bool powerOff();

  const BoardConfig& board_;
  bool transportOk_ = false;
  bool sessionActive_ = false;
  bool initialized_ = false;
  uint32_t lastRefreshDurationMs_ = 0;
  const char* lastError_ = "";
};

}  // namespace inktime

#endif
