#include "spectra6_73.h"

#if INKTIME_PHOTOPAINTER_ENABLED

#include <driver/gpio.h>

// The controller initialization values are derived from Waveshare's
// ESP32-S3-PhotoPainter xiaozhi subtree (MIT). InkTime adds bounded BUSY waits,
// failure propagation and power-off around Waveshare's persistent ESP-IDF
// SPI3 half-duplex transport.

namespace inktime {

namespace {

constexpr size_t kSpiTransferChunkBytes = 5000U;
spi_device_handle_t gEpdDevice = nullptr;
bool gEpdBusInitialized = false;
bool gEpdTransportReady = false;

}  // namespace

bool prepareSpectra6ColdBootTransport(const BoardConfig& board) {
  if (gEpdTransportReady && gEpdDevice != nullptr) return true;

  spi_bus_config_t busConfig = {};
  busConfig.miso_io_num = -1;
  busConfig.mosi_io_num = board.display.spi.mosi;
  busConfig.sclk_io_num = board.display.spi.sck;
  busConfig.quadwp_io_num = -1;
  busConfig.quadhd_io_num = -1;
  busConfig.max_transfer_sz = static_cast<int>(board.display.width * board.display.height);
  if (spi_bus_initialize(SPI3_HOST, &busConfig, SPI_DMA_CH_AUTO) != ESP_OK) return false;
  gEpdBusInitialized = true;

  spi_device_interface_config_t deviceConfig = {};
  deviceConfig.spics_io_num = -1;
  deviceConfig.clock_speed_hz = static_cast<int>(board.display.clockHz);
  deviceConfig.mode = 0;
  deviceConfig.queue_size = 7;
  deviceConfig.flags = SPI_DEVICE_HALFDUPLEX;
  if (spi_bus_add_device(SPI3_HOST, &deviceConfig, &gEpdDevice) != ESP_OK) {
    (void)spi_bus_free(SPI3_HOST);
    gEpdBusInitialized = false;
    return false;
  }
  gEpdTransportReady = true;
  return true;
}

Spectra6_73::Spectra6_73(const BoardConfig& board) : board_(board) {}

bool Spectra6_73::waitForBusyAssertion(uint32_t timeoutMs) {
  const int busyLevel = board_.display.busyActiveLow ? LOW : HIGH;
  const uint32_t started = millis();
  while (gpio_get_level(static_cast<gpio_num_t>(board_.display.busy)) != busyLevel) {
    if (millis() - started >= timeoutMs) {
      lastError_ = "EPD-BUSY-NOT-ASSERTED";
      return false;
    }
    delay(1);
  }
  return true;
}

bool Spectra6_73::waitUntilReady(uint32_t timeoutMs) {
  const int busyLevel = board_.display.busyActiveLow ? LOW : HIGH;
  const uint32_t started = millis();
  while (gpio_get_level(static_cast<gpio_num_t>(board_.display.busy)) == busyLevel) {
    if (millis() - started >= timeoutMs) {
      lastError_ = "EPD-BUSY-TIMEOUT";
      return false;
    }
    delay(5);  // yields to the ESP32 scheduler/watchdog
  }
  return true;
}

bool Spectra6_73::waitForBusyCycle() {
  return waitForBusyAssertion() && waitUntilReady();
}

void Spectra6_73::sendCommand(uint8_t command) {
  gpio_set_level(static_cast<gpio_num_t>(board_.display.dc), 0);
  gpio_set_level(static_cast<gpio_num_t>(board_.display.spi.cs), 0);
  (void)transmit(&command, 1U);
  gpio_set_level(static_cast<gpio_num_t>(board_.display.spi.cs), 1);
}

void Spectra6_73::sendData(uint8_t data) {
  gpio_set_level(static_cast<gpio_num_t>(board_.display.dc), 1);
  gpio_set_level(static_cast<gpio_num_t>(board_.display.spi.cs), 0);
  (void)transmit(&data, 1U);
  gpio_set_level(static_cast<gpio_num_t>(board_.display.spi.cs), 1);
}

void Spectra6_73::sendData(const uint8_t* data, size_t length) {
  if (data == nullptr || length == 0U) return;
  gpio_set_level(static_cast<gpio_num_t>(board_.display.dc), 1);
  gpio_set_level(static_cast<gpio_num_t>(board_.display.spi.cs), 0);
  size_t offset = 0;
  while (offset < length) {
    const size_t remaining = length - offset;
    const size_t chunk = remaining < kSpiTransferChunkBytes
        ? remaining
        : kSpiTransferChunkBytes;
    if (!transmit(data + offset, chunk)) break;
    offset += chunk;
  }
  gpio_set_level(static_cast<gpio_num_t>(board_.display.spi.cs), 1);
}

bool Spectra6_73::transmit(const uint8_t* data, size_t length) {
  if (!transportOk_ || !gEpdTransportReady || gEpdDevice == nullptr
      || data == nullptr || length == 0U) {
    lastError_ = "EPD-SPI-WRITE";
    transportOk_ = false;
    return false;
  }
  spi_transaction_t transaction = {};
  transaction.length = static_cast<uint32_t>(length * 8U);
  transaction.tx_buffer = data;
  if (spi_device_polling_transmit(gEpdDevice, &transaction) != ESP_OK) {
    lastError_ = "EPD-SPI-WRITE";
    transportOk_ = false;
    return false;
  }
  return true;
}

void Spectra6_73::hardwareReset() {
  gpio_set_level(static_cast<gpio_num_t>(board_.display.reset), 1);
  delay(50);
  gpio_set_level(static_cast<gpio_num_t>(board_.display.reset), 0);
  delay(20);
  gpio_set_level(static_cast<gpio_num_t>(board_.display.reset), 1);
  delay(50);
}

bool Spectra6_73::begin() {
  lastError_ = "";
  initialized_ = false;
  transportOk_ = gEpdTransportReady && gEpdDevice != nullptr && gEpdBusInitialized;
  gpio_set_level(static_cast<gpio_num_t>(board_.display.spi.cs), 1);
  gpio_set_level(static_cast<gpio_num_t>(board_.display.reset), 1);

  if (!transportOk_) {
    lastError_ = "EPD-SPI-INIT";
    return false;
  }
  sessionActive_ = true;

  hardwareReset();
  if (!waitUntilReady()) {
    safeShutdown();
    return false;
  }
  delay(50);

  sendCommand(0xAA);
  // Match Waveshare's validated factory framing: every 0xAA parameter is a
  // separate data transaction with its own CS low/high interval.
  sendData(0x49);
  sendData(0x55);
  sendData(0x20);
  sendData(0x08);
  sendData(0x09);
  sendData(0x18);
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
  if (!transportOk_) {
    safeShutdown();
    return false;
  }
  // Waveshare's controller can complete POWER_ON before BUSY is sampled.
  // Treat a high/ready BUSY as complete here, but require an observed BUSY
  // cycle for DISPLAY_REFRESH below so an unpowered panel cannot pass.
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
  return transportOk_ && waitUntilReady();
}

bool Spectra6_73::displayFrame(const uint8_t* framebuffer, size_t length) {
  if (!initialized_ || framebuffer == nullptr || length != frameBufferBytes(board_)) {
    lastError_ = "EPD-FRAMEBUFFER";
    safeShutdown();
    return false;
  }

  sendCommand(0x10);
  sendData(framebuffer, length);
  if (!transportOk_) {
    safeShutdown();
    return false;
  }

  sendCommand(0x04);
  if (!waitUntilReady()) {
    safeShutdown();
    return false;
  }
  sendCommand(0x06);
  sendData(0x6F); sendData(0x1F); sendData(0x17); sendData(0x49);
  sendCommand(0x12);
  sendData(0x00);
  if (!transportOk_) {
    safeShutdown();
    return false;
  }
  const uint32_t refreshStarted = millis();
  if (!waitForBusyCycle()) {
    safeShutdown();
    return false;
  }
  lastRefreshDurationMs_ = millis() - refreshStarted;

  if (!powerOff()) {
    safeShutdown();
    return false;
  }
  sessionActive_ = false;
  initialized_ = false;
  return true;
}

void Spectra6_73::safeShutdown() {
  if (sessionActive_) {
    const int busyLevel = board_.display.busyActiveLow ? LOW : HIGH;
    if (gpio_get_level(static_cast<gpio_num_t>(board_.display.busy)) != busyLevel) {
      sendCommand(0x02);
      sendData(0x00);
    }
    gpio_set_level(static_cast<gpio_num_t>(board_.display.spi.cs), 1);
    gpio_set_level(static_cast<gpio_num_t>(board_.display.reset), 0);
  }
  transportOk_ = gEpdTransportReady && gEpdDevice != nullptr;
  sessionActive_ = false;
  initialized_ = false;
}

}  // namespace inktime

#endif
