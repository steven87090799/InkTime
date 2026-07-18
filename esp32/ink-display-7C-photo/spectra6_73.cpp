#include "spectra6_73.h"

#if INKTIME_PHOTOPAINTER_ENABLED

// The controller initialization values are derived from Waveshare's
// ESP32-S3-PhotoPainter xiaozhi subtree (MIT). InkTime adds bounded BUSY waits,
// failure propagation, conservative 4 MHz SPI, power-off, and deep sleep.

namespace inktime {

Spectra6_73::Spectra6_73(SPIClass& spi, const BoardConfig& board)
    : spi_(spi), board_(board) {}

bool Spectra6_73::waitUntilReady(uint32_t timeoutMs) {
  const int busyLevel = board_.display.busyActiveLow ? LOW : HIGH;
  const uint32_t started = millis();
  while (digitalRead(board_.display.busy) == busyLevel) {
    if (millis() - started >= timeoutMs) {
      lastError_ = "EPD-BUSY-TIMEOUT";
      return false;
    }
    delay(5);  // yields to the ESP32 scheduler/watchdog
  }
  return true;
}

void Spectra6_73::sendCommand(uint8_t command) {
  digitalWrite(board_.display.dc, LOW);
  digitalWrite(board_.display.spi.cs, LOW);
  spi_.transfer(command);
  digitalWrite(board_.display.spi.cs, HIGH);
}

void Spectra6_73::sendData(uint8_t data) {
  digitalWrite(board_.display.dc, HIGH);
  digitalWrite(board_.display.spi.cs, LOW);
  spi_.transfer(data);
  digitalWrite(board_.display.spi.cs, HIGH);
}

void Spectra6_73::sendData(const uint8_t* data, size_t length) {
  digitalWrite(board_.display.dc, HIGH);
  digitalWrite(board_.display.spi.cs, LOW);
  for (size_t offset = 0; offset < length; ++offset) {
    spi_.transfer(data[offset]);
    if ((offset & 0x03FFU) == 0) yield();
  }
  digitalWrite(board_.display.spi.cs, HIGH);
}

void Spectra6_73::hardwareReset() {
  digitalWrite(board_.display.reset, HIGH);
  delay(50);
  digitalWrite(board_.display.reset, LOW);
  delay(20);
  digitalWrite(board_.display.reset, HIGH);
  delay(50);
}

bool Spectra6_73::begin() {
  lastError_ = "";
  initialized_ = false;
  pinMode(board_.display.spi.cs, OUTPUT);
  pinMode(board_.display.dc, OUTPUT);
  pinMode(board_.display.reset, OUTPUT);
  pinMode(board_.display.busy, INPUT_PULLUP);
  digitalWrite(board_.display.spi.cs, HIGH);
  digitalWrite(board_.display.reset, HIGH);

  spi_.begin(
    board_.display.spi.sck,
    board_.display.spi.miso,
    board_.display.spi.mosi,
    board_.display.spi.cs
  );
  spi_.beginTransaction(SPISettings(board_.display.clockHz, MSBFIRST, SPI_MODE0));
  sessionActive_ = true;

  hardwareReset();
  if (!waitUntilReady()) {
    safeShutdown();
    return false;
  }

  sendCommand(0xAA);
  const uint8_t commandHeader[] = {0x49, 0x55, 0x20, 0x08, 0x09, 0x18};
  sendData(commandHeader, sizeof(commandHeader));
  sendCommand(0x01); sendData(0x3F);
  sendCommand(0x00); sendData(0x5F); sendData(0x69);
  sendCommand(0x03); sendData(0x00); sendData(0x54); sendData(0x00); sendData(0x44);
  sendCommand(0x05); sendData(0x40); sendData(0x1F); sendData(0x1F); sendData(0x2C);
  sendCommand(0x06); sendData(0x6F); sendData(0x1F); sendData(0x17); sendData(0x49);
  sendCommand(0x08); sendData(0x6F); sendData(0x1F); sendData(0x1F); sendData(0x22);
  sendCommand(0x30); sendData(0x03);
  sendCommand(0x50); sendData(0x3F);
  sendCommand(0x60); sendData(0x02); sendData(0x00);
  sendCommand(0x61);
  sendData(static_cast<uint8_t>(board_.display.width >> 8U));
  sendData(static_cast<uint8_t>(board_.display.width & 0xFFU));
  sendData(static_cast<uint8_t>(board_.display.height >> 8U));
  sendData(static_cast<uint8_t>(board_.display.height & 0xFFU));
  sendCommand(0x84); sendData(0x01);
  sendCommand(0xE3); sendData(0x2F);
  sendCommand(0x04);
  if (!waitUntilReady()) {
    safeShutdown();
    return false;
  }
  initialized_ = true;
  return true;
}

bool Spectra6_73::powerOff() {
  if (!sessionActive_) return true;
  sendCommand(0x02);
  sendData(0x00);
  return waitUntilReady();
}

void Spectra6_73::deepSleep() {
  if (!sessionActive_) return;
  sendCommand(0x07);
  sendData(0xA5);
}

bool Spectra6_73::displayFrame(const uint8_t* framebuffer, size_t length) {
  if (!initialized_ || framebuffer == nullptr || length != frameBufferBytes(board_)) {
    lastError_ = "EPD-FRAMEBUFFER";
    safeShutdown();
    return false;
  }

  sendCommand(0x10);
  sendData(framebuffer, length);

  sendCommand(0x04);
  if (!waitUntilReady()) {
    safeShutdown();
    return false;
  }
  sendCommand(0x06);
  sendData(0x6F); sendData(0x1F); sendData(0x17); sendData(0x49);
  sendCommand(0x12);
  sendData(0x00);
  const uint32_t refreshStarted = millis();
  if (!waitUntilReady()) {
    safeShutdown();
    return false;
  }
  lastRefreshDurationMs_ = millis() - refreshStarted;

  if (!powerOff()) {
    safeShutdown();
    return false;
  }
  deepSleep();
  spi_.endTransaction();
  spi_.end();
  sessionActive_ = false;
  initialized_ = false;
  return true;
}

void Spectra6_73::safeShutdown() {
  if (sessionActive_) {
    const int busyLevel = board_.display.busyActiveLow ? LOW : HIGH;
    if (digitalRead(board_.display.busy) != busyLevel) {
      sendCommand(0x02);
      sendData(0x00);
      deepSleep();
    }
    digitalWrite(board_.display.spi.cs, HIGH);
    digitalWrite(board_.display.reset, LOW);
    spi_.endTransaction();
    spi_.end();
  }
  sessionActive_ = false;
  initialized_ = false;
}

}  // namespace inktime

#endif
