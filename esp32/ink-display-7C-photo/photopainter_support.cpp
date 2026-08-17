#include "photopainter_support.h"

#if INKTIME_PHOTOPAINTER_ENABLED

#include <Arduino.h>
#include <ArduinoJson.h>
#include <FS.h>
#include <SD.h>
#include <SPI.h>
#include <Wire.h>
#include <driver/rtc_io.h>
#include <esp_heap_caps.h>
#include <esp_sleep.h>
#include <new>
#include <sys/time.h>

#include "photopainter_core.h"
#include "photopainter_wake_core.h"
#include "power_manager.h"
#include "spectra6_73.h"

#ifndef INKTIME_DEBUG_LOG
#define INKTIME_DEBUG_LOG 0
#endif

// Arduino-ESP32 3.x names the ESP32-S3 SPI2/SPI3 hosts FSPI/HSPI. Older
// compatible cores may expose the IDF host constants only.
#ifndef FSPI
#define FSPI SPI2_HOST
#endif
#ifndef HSPI
#define HSPI SPI3_HOST
#endif

extern HardwareSerial DebugSerial;

namespace inktime {

constexpr uint8_t kAxp2101Address = 0x34;
constexpr uint8_t kAxp2101ChipIdRegister = 0x03;
constexpr uint8_t kAxp2101ChipId = 0x4A;
constexpr uint8_t kAxp2101Status1 = 0x00;
constexpr uint8_t kAxp2101Status2 = 0x01;
constexpr uint8_t kAxp2101BatteryVoltageHigh = 0x34;
constexpr uint8_t kAxp2101BatteryVoltageLow = 0x35;
constexpr uint8_t kAxp2101BatteryPercent = 0xA4;
constexpr uint8_t kShtc3Address = 0x70;
constexpr uint8_t kPcf85063Address = 0x51;
constexpr size_t kIoChunkSize = 4096;
constexpr uint32_t kI2cTimeoutMs = 50;
constexpr uint8_t kI2cMaximumAttempts = 3U;
constexpr uint32_t kI2cRetryDelayMs = 2U;

const uint8_t kBlankGlyph[5] = {0, 0, 0, 0, 0};
const uint8_t kFontDigits[10][5] = {
  {0x3E,0x51,0x49,0x45,0x3E}, {0x00,0x42,0x7F,0x40,0x00},
  {0x62,0x51,0x49,0x49,0x46}, {0x22,0x49,0x49,0x49,0x36},
  {0x18,0x14,0x12,0x7F,0x10}, {0x2F,0x49,0x49,0x49,0x31},
  {0x3E,0x49,0x49,0x49,0x32}, {0x01,0x71,0x09,0x05,0x03},
  {0x36,0x49,0x49,0x49,0x36}, {0x26,0x49,0x49,0x49,0x3E},
};
const uint8_t kFontUpper[26][5] = {
  {0x7E,0x11,0x11,0x11,0x7E}, {0x7F,0x49,0x49,0x49,0x36},
  {0x3E,0x41,0x41,0x41,0x22}, {0x7F,0x41,0x41,0x22,0x1C},
  {0x7F,0x49,0x49,0x49,0x41}, {0x7F,0x09,0x09,0x09,0x01},
  {0x3E,0x41,0x49,0x49,0x7A}, {0x7F,0x08,0x08,0x08,0x7F},
  {0x00,0x41,0x7F,0x41,0x00}, {0x20,0x40,0x41,0x3F,0x01},
  {0x7F,0x08,0x14,0x22,0x41}, {0x7F,0x40,0x40,0x40,0x40},
  {0x7F,0x02,0x0C,0x02,0x7F}, {0x7F,0x04,0x08,0x10,0x7F},
  {0x3E,0x41,0x41,0x41,0x3E}, {0x7F,0x09,0x09,0x09,0x06},
  {0x3E,0x41,0x51,0x21,0x5E}, {0x7F,0x09,0x19,0x29,0x46},
  {0x46,0x49,0x49,0x49,0x31}, {0x01,0x01,0x7F,0x01,0x01},
  {0x3F,0x40,0x40,0x40,0x3F}, {0x1F,0x20,0x40,0x20,0x1F},
  {0x3F,0x40,0x38,0x40,0x3F}, {0x63,0x14,0x08,0x14,0x63},
  {0x07,0x08,0x70,0x08,0x07}, {0x61,0x51,0x49,0x45,0x43},
};

const uint8_t* pairingGlyph(char value) {
  if (value >= '0' && value <= '9') return kFontDigits[value - '0'];
  if (value >= 'A' && value <= 'Z') return kFontUpper[value - 'A'];
  static const uint8_t colon[5] = {0x00,0x36,0x36,0x00,0x00};
  static const uint8_t dash[5] = {0x08,0x08,0x08,0x08,0x08};
  static const uint8_t slash[5] = {0x02,0x04,0x08,0x10,0x20};
  static const uint8_t dot[5] = {0x00,0x00,0x60,0x60,0x00};
  if (value == ':') return colon;
  if (value == '-') return dash;
  if (value == '/') return slash;
  if (value == '.') return dot;
  return kBlankGlyph;
}

void drawPairingText(uint8_t* frame, size_t length, int x, int y, const String& raw, uint8_t scale) {
  String text = raw;
  text.toUpperCase();
  for (size_t index = 0; index < text.length(); ++index) {
    const uint8_t* glyph = pairingGlyph(text[index]);
    const int origin = x + static_cast<int>(index) * static_cast<int>(6U * scale);
    if (origin + static_cast<int>(5U * scale) >= static_cast<int>(kPhotoPainterWidth)) break;
    for (uint8_t column = 0; column < 5; ++column) {
      for (uint8_t row = 0; row < 7; ++row) {
        if ((glyph[column] & (1U << row)) == 0) continue;
        for (uint8_t dx = 0; dx < scale; ++dx) {
          for (uint8_t dy = 0; dy < scale; ++dy) {
            writePacked4(frame, length, kPhotoPainterWidth,
              static_cast<uint16_t>(origin + column * scale + dx),
              static_cast<uint16_t>(y + row * scale + dy), 0);
          }
        }
      }
    }
  }
}

#if INKTIME_DEBUG_LOG
#define PP_LOG(...) DebugSerial.printf(__VA_ARGS__)
#else
#define PP_LOG(...) do { } while (0)
#endif

struct I2cRetryTelemetry {
  uint32_t retry_count = 0;
  uint32_t bus_reset_count = 0;
  uint32_t fail_closed_count = 0;
};

class BoundedI2cBus final {
 public:
  BoundedI2cBus(TwoWire& wire, const I2cConfig& config, I2cRetryTelemetry& telemetry)
      : wire_(wire), config_(config), telemetry_(telemetry) {}

  bool probe(uint8_t address) {
    return run(true, [&]() {
      wire_.beginTransmission(address);
      return wire_.endTransmission() == 0;
    });
  }

  bool writeCommand(uint8_t address, uint16_t command, bool replaySafe) {
    return run(replaySafe, [&]() {
      wire_.beginTransmission(address);
      if (wire_.write(static_cast<uint8_t>(command >> 8U)) != 1U
          || wire_.write(static_cast<uint8_t>(command & 0xFFU)) != 1U) {
        return false;
      }
      return wire_.endTransmission() == 0;
    });
  }

  bool readRegister(
    uint8_t address,
    uint8_t reg,
    uint8_t* data,
    size_t length
  ) {
    if (data == nullptr || length == 0 || length > 32) return false;
    return run(true, [&]() {
      wire_.beginTransmission(address);
      if (wire_.write(reg) != 1U || wire_.endTransmission(false) != 0) return false;
      const size_t received = wire_.requestFrom(address, static_cast<uint8_t>(length), true);
      if (received != length) return false;
      for (size_t index = 0; index < length; ++index) {
        if (!wire_.available()) return false;
        data[index] = static_cast<uint8_t>(wire_.read());
      }
      return true;
    });
  }

  bool readBytes(uint8_t address, uint8_t* data, size_t length) {
    if (data == nullptr || length == 0 || length > 32) return false;
    return run(true, [&]() {
      const size_t received = wire_.requestFrom(address, static_cast<uint8_t>(length), true);
      if (received != length) return false;
      for (size_t index = 0; index < length; ++index) {
        if (!wire_.available()) return false;
        data[index] = static_cast<uint8_t>(wire_.read());
      }
      return true;
    });
  }

  bool writeRegisters(
    uint8_t address,
    uint8_t firstRegister,
    const uint8_t* data,
    size_t length,
    bool replaySafe
  ) {
    if (data == nullptr || length == 0 || length > 24) return false;
    return run(replaySafe, [&]() {
      wire_.beginTransmission(address);
      if (wire_.write(firstRegister) != 1U || wire_.write(data, length) != length) {
        return false;
      }
      return wire_.endTransmission() == 0;
    });
  }

 private:
  static void increment(uint32_t& value) {
    if (value < UINT32_MAX) ++value;
  }

  template <typename Operation>
  bool run(bool replaySafe, Operation operation) {
    for (uint8_t attempt = 0; attempt < kI2cMaximumAttempts; ++attempt) {
      if (operation()) return true;
      if (!replaySafe || attempt + 1U >= kI2cMaximumAttempts) {
        increment(telemetry_.fail_closed_count);
        return false;
      }
      increment(telemetry_.retry_count);
      if (attempt == 0U) {
        delay(kI2cRetryDelayMs);
      } else {
        // Reset/re-init is deliberately allowed only once, between attempts 2 and 3.
        increment(telemetry_.bus_reset_count);
        if (!resetBus()) {
          increment(telemetry_.fail_closed_count);
          return false;
        }
      }
    }
    increment(telemetry_.fail_closed_count);
    return false;
  }

  bool resetBus() {
    wire_.end();
    delay(kI2cRetryDelayMs);
    const bool ready = wire_.begin(config_.sda, config_.scl, config_.clockHz);
    if (ready) wire_.setTimeOut(kI2cTimeoutMs);
    return ready;
  }

  TwoWire& wire_;
  const I2cConfig& config_;
  I2cRetryTelemetry& telemetry_;
};

uint8_t toBcd(uint8_t value) {
  return static_cast<uint8_t>(((value / 10U) << 4U) | (value % 10U));
}

uint8_t fromBcd(uint8_t value) {
  return static_cast<uint8_t>((value >> 4U) * 10U + (value & 0x0FU));
}

int64_t daysFromCivil(int year, unsigned month, unsigned day) {
  year -= month <= 2;
  const int era = (year >= 0 ? year : year - 399) / 400;
  const unsigned yearOfEra = static_cast<unsigned>(year - era * 400);
  const unsigned adjustedMonth = month > 2 ? month - 3U : month + 9U;
  const unsigned dayOfYear = (153U * adjustedMonth + 2U) / 5U
                           + day - 1U;
  const unsigned dayOfEra = yearOfEra * 365U + yearOfEra / 4U - yearOfEra / 100U
                          + dayOfYear;
  return static_cast<int64_t>(era) * 146097 + static_cast<int64_t>(dayOfEra) - 719468;
}

class ProbePowerManager final : public PowerManager {
 public:
  explicit ProbePowerManager(BoundedI2cBus& bus) : bus_(bus) {}

  bool begin() override {
    type_ = PmicType::None;
    if (!bus_.probe(kAxp2101Address)) return false;
    uint8_t chipId = 0;
    if (!bus_.readRegister(kAxp2101Address, kAxp2101ChipIdRegister, &chipId, 1)) {
      type_ = PmicType::Unknown;
      return false;
    }
    if (chipId != kAxp2101ChipId) {
      type_ = PmicType::Unknown;
      return false;
    }
    type_ = PmicType::AXP2101;
    refreshMeasurements();
    return true;
  }

  void refreshMeasurements() override {
    usbConnected_ = false;
    batteryMillivolts_ = 0;
    batteryPercent_ = -1;
    if (type_ != PmicType::AXP2101) return;
    uint8_t status[2] = {0, 0};
    if (!bus_.readRegister(kAxp2101Address, kAxp2101Status1, status, 2)) return;
    const bool batteryConnected = (status[0] & (1U << 3U)) != 0;
    const bool vbusGood = (status[0] & (1U << 5U)) != 0;
    const bool vbusOverVoltage = (status[1] & (1U << 3U)) != 0;
    usbConnected_ = vbusGood && !vbusOverVoltage;
    if (!batteryConnected) return;
    uint8_t voltage[2] = {0, 0};
    if (bus_.readRegister(kAxp2101Address, kAxp2101BatteryVoltageHigh, voltage, 2)) {
      batteryMillivolts_ = static_cast<uint16_t>((voltage[0] & 0x1FU) << 8U)
                         | voltage[1];
    }
    uint8_t percent = 0;
    if (bus_.readRegister(kAxp2101Address, kAxp2101BatteryPercent, &percent, 1)
        && percent <= 100) {
      batteryPercent_ = percent;
    }
  }

  PmicType type() const override { return type_; }
  bool isUsbConnected() const override { return usbConnected_; }
  float batteryVoltage() const override { return batteryMillivolts_ / 1000.0f; }
  int batteryPercent() const override { return batteryPercent_; }
  void prepareForDeepSleep() override {
    // Deliberately read-only: board revisions must be identified before any
    // PMIC rail voltage or shutdown-register writes are enabled.
  }

 private:
  BoundedI2cBus& bus_;
  PmicType type_ = PmicType::None;
  bool usbConnected_ = false;
  uint16_t batteryMillivolts_ = 0;
  int batteryPercent_ = -1;
};

class Shtc3Adapter {
 public:
  explicit Shtc3Adapter(BoundedI2cBus& bus) : bus_(bus) {}

  bool begin() {
    ready_ = false;
    if (!bus_.probe(kShtc3Address) || !bus_.writeCommand(kShtc3Address, 0x3517, true)) {
      return false;
    }
    delay(1);
    if (!bus_.writeCommand(kShtc3Address, 0xEFC8, true)) {
      sleep();
      return false;
    }
    delay(2);
    uint8_t id[3] = {0, 0, 0};
    if (!bus_.readBytes(kShtc3Address, id, sizeof(id))) {
      sleep();
      return false;
    }
    ready_ = shtc3Crc8(id, 2) == id[2]
          && ((static_cast<uint16_t>(id[0]) << 8U | id[1]) & 0x083FU) == 0x0807U;
    sleep();
    return ready_;
  }

  bool read(float& temperatureC, float& humidityPercent) {
    if (!ready_ || !bus_.writeCommand(kShtc3Address, 0x3517, true)) return false;
    delay(1);
    if (!bus_.writeCommand(kShtc3Address, 0x7CA2, true)) {
      sleep();
      return false;
    }
    const uint32_t started = millis();
    while (millis() - started < 15) delay(1);
    uint8_t bytes[6] = {0, 0, 0, 0, 0, 0};
    if (!bus_.readBytes(kShtc3Address, bytes, sizeof(bytes))) {
      sleep();
      return false;
    }
    sleep();
    if (shtc3Crc8(bytes, 2) != bytes[2] || shtc3Crc8(bytes + 3, 2) != bytes[5]) {
      return false;
    }
    const uint16_t rawTemperature = static_cast<uint16_t>(bytes[0]) << 8U | bytes[1];
    const uint16_t rawHumidity = static_cast<uint16_t>(bytes[3]) << 8U | bytes[4];
    temperatureC = -45.0f + 175.0f * rawTemperature / 65535.0f;
    humidityPercent = 100.0f * rawHumidity / 65535.0f;
    return humidityPercent >= 0.0f && humidityPercent <= 100.0f;
  }

 private:
  void sleep() { (void)bus_.writeCommand(kShtc3Address, 0xB098, true); }
  BoundedI2cBus& bus_;
  bool ready_ = false;
};

class Pcf85063Adapter {
 public:
  explicit Pcf85063Adapter(BoundedI2cBus& bus) : bus_(bus) {}

  bool begin() {
    ready_ = bus_.probe(kPcf85063Address);
    return ready_;
  }

  bool writeEpoch(time_t epoch) {
    if (!ready_ || epoch <= 0) return false;
    struct tm utc = {};
    gmtime_r(&epoch, &utc);
    if (utc.tm_year < 100 || utc.tm_year > 199) return false;
    const uint8_t registers[] = {
      toBcd(static_cast<uint8_t>(utc.tm_sec)),
      toBcd(static_cast<uint8_t>(utc.tm_min)),
      toBcd(static_cast<uint8_t>(utc.tm_hour)),
      toBcd(static_cast<uint8_t>(utc.tm_mday)),
      toBcd(static_cast<uint8_t>(utc.tm_wday)),
      toBcd(static_cast<uint8_t>(utc.tm_mon + 1)),
      toBcd(static_cast<uint8_t>(utc.tm_year - 100)),
    };
    // Setting the complete RTC register bank is deterministic; replaying the
    // same value after an I2C transport failure cannot increment or toggle state.
    return bus_.writeRegisters(
      kPcf85063Address, 0x04, registers, sizeof(registers), true);
  }

  bool readEpoch(time_t& epoch) {
    epoch = 0;
    if (!ready_) return false;
    uint8_t registers[7] = {0};
    if (!bus_.readRegister(kPcf85063Address, 0x04, registers, sizeof(registers))
        || (registers[0] & 0x80U) != 0) {
      return false;
    }
    struct tm utc = {};
    utc.tm_sec = fromBcd(registers[0] & 0x7FU);
    utc.tm_min = fromBcd(registers[1] & 0x7FU);
    utc.tm_hour = fromBcd(registers[2] & 0x3FU);
    utc.tm_mday = fromBcd(registers[3] & 0x3FU);
    utc.tm_mon = fromBcd(registers[5] & 0x1FU) - 1;
    utc.tm_year = fromBcd(registers[6]) + 100;
    if (utc.tm_sec > 59 || utc.tm_min > 59 || utc.tm_hour > 23
        || utc.tm_mday < 1 || utc.tm_mday > 31 || utc.tm_mon < 0 || utc.tm_mon > 11) {
      return false;
    }
    const int year = utc.tm_year + 1900;
    const unsigned month = static_cast<unsigned>(utc.tm_mon + 1);
    const int64_t seconds = daysFromCivil(year, month, static_cast<unsigned>(utc.tm_mday))
                          * 86400LL + utc.tm_hour * 3600LL + utc.tm_min * 60LL + utc.tm_sec;
    epoch = static_cast<time_t>(seconds);
    return epoch > 0;
  }

 private:
  BoundedI2cBus& bus_;
  bool ready_ = false;
};

bool makeCachePaths(
  uint32_t sourceHash,
  DisplayRotation rotation,
  char* finalPath,
  char* temporaryPath,
  char* backupPath,
  size_t capacity
) {
  const unsigned rotationValue = static_cast<unsigned>(rotation);
  const int finalLength = snprintf(
    finalPath, capacity, "/cache/%08lx-r%u.itfc",
    static_cast<unsigned long>(sourceHash), rotationValue);
  const int temporaryLength = snprintf(
    temporaryPath, capacity, "/cache/%08lx-r%u.tmp",
    static_cast<unsigned long>(sourceHash), rotationValue);
  const int backupLength = snprintf(
    backupPath, capacity, "/cache/%08lx-r%u.bak",
    static_cast<unsigned long>(sourceHash), rotationValue);
  return finalLength > 0 && temporaryLength > 0 && backupLength > 0
      && static_cast<size_t>(finalLength) < capacity
      && static_cast<size_t>(temporaryLength) < capacity
      && static_cast<size_t>(backupLength) < capacity;
}

bool makeFormalFramePaths(
  const char* sourceSha256,
  DisplayRotation rotation,
  char* finalPath,
  char* temporaryPath,
  char* backupPath,
  size_t capacity
) {
  if (!isSha256Hex(sourceSha256) || finalPath == nullptr || temporaryPath == nullptr
      || backupPath == nullptr || capacity == 0) {
    return false;
  }
  const unsigned rotationValue = static_cast<unsigned>(rotation);
  const int finalLength = snprintf(
    finalPath, capacity, "/inktime/frames/%s-r%u.itf", sourceSha256, rotationValue);
  const int temporaryLength = snprintf(
    temporaryPath, capacity, "/inktime/frames/%s-r%u.tmp", sourceSha256, rotationValue);
  const int backupLength = snprintf(
    backupPath, capacity, "/inktime/frames/%s-r%u.bak", sourceSha256, rotationValue);
  return finalLength > 0 && temporaryLength > 0 && backupLength > 0
      && static_cast<size_t>(finalLength) < capacity
      && static_cast<size_t>(temporaryLength) < capacity
      && static_cast<size_t>(backupLength) < capacity;
}

bool makeActiveSchedulePaths(
  char* finalPath,
  char* temporaryPath,
  char* backupPath,
  size_t capacity
) {
  if (finalPath == nullptr || temporaryPath == nullptr || backupPath == nullptr || capacity == 0) {
    return false;
  }
  const int finalLength = snprintf(finalPath, capacity, "/inktime/schedule/active.json");
  const int temporaryLength = snprintf(temporaryPath, capacity, "/inktime/schedule/active.tmp");
  const int backupLength = snprintf(backupPath, capacity, "/inktime/schedule/active.bak");
  return finalLength > 0 && temporaryLength > 0 && backupLength > 0
      && static_cast<size_t>(finalLength) < capacity
      && static_cast<size_t>(temporaryLength) < capacity
      && static_cast<size_t>(backupLength) < capacity;
}

bool makeStagedNextSchedulePaths(
  char* finalPath,
  char* temporaryPath,
  char* backupPath,
  size_t capacity
) {
  if (finalPath == nullptr || temporaryPath == nullptr || backupPath == nullptr || capacity == 0) {
    return false;
  }
  const int finalLength = snprintf(finalPath, capacity, "/inktime/schedule/staged_next.json");
  const int temporaryLength = snprintf(temporaryPath, capacity, "/inktime/schedule/staged_next.tmp");
  const int backupLength = snprintf(backupPath, capacity, "/inktime/schedule/staged_next.bak");
  return finalLength > 0 && temporaryLength > 0 && backupLength > 0
      && static_cast<size_t>(finalLength) < capacity
      && static_cast<size_t>(temporaryLength) < capacity
      && static_cast<size_t>(backupLength) < capacity;
}

struct PhotoPainterSupport::Impl {
  explicit Impl(const BoardConfig& board)
      : epdSpi(FSPI),
        sdSpi(HSPI),
        display(epdSpi, board),
        i2c(Wire, board.i2c, i2cTelemetry),
        power(i2c),
        sensor(i2c),
        rtc(i2c) {}

  SPIClass epdSpi;
  SPIClass sdSpi;
  Spectra6_73 display;
  I2cRetryTelemetry i2cTelemetry;
  BoundedI2cBus i2c;
  ProbePowerManager power;
  Shtc3Adapter sensor;
  Pcf85063Adapter rtc;
  uint8_t* ioBuffer = nullptr;
};

const char* cacheStatusName(CacheStatus status) {
  switch (status) {
    case CacheStatus::Disabled: return "disabled";
    case CacheStatus::Miss: return "miss";
    case CacheStatus::Hit: return "hit";
    case CacheStatus::Written: return "written";
    case CacheStatus::Invalid: return "invalid";
    case CacheStatus::Error: return "error";
  }
  return "error";
}

PhotoPainterSupport::PhotoPainterSupport(const BoardConfig& board) : board_(board) {}

PhotoPainterSupport::~PhotoPainterSupport() {
  if (impl_ != nullptr) {
    if (impl_->ioBuffer != nullptr) heap_caps_free(impl_->ioBuffer);
    delete impl_;
  }
}

bool PhotoPainterSupport::begin() {
  if (impl_ != nullptr) return hardwareReady_;
  impl_ = new (std::nothrow) Impl(board_);
  if (impl_ == nullptr) {
    lastError_ = "BOARD-MEMORY";
    return false;
  }

  const size_t flashSize = ESP.getFlashChipSize();
  const size_t psramSize = ESP.getPsramSize();
  flashReady_ = board_.requiredFlashBytes == 0 || flashSize >= board_.requiredFlashBytes;
  psramReady_ = psramFound()
      && (board_.requiredPsramBytes == 0 || psramSize >= board_.requiredPsramBytes);
  hardwareReady_ = flashReady_ && psramReady_;
  PP_LOG("[BOARD] profile=%s flash=%u psram=%u ready=%d\n",
         board_.name, flashSize, psramSize, hardwareReady_ ? 1 : 0);

  const esp_sleep_wakeup_cause_t wakeCause = esp_sleep_get_wakeup_cause();
  const uint64_t ext1WakeStatus = wakeCause == ESP_SLEEP_WAKEUP_EXT1
      ? esp_sleep_get_ext1_wakeup_status()
      : 0ULL;
  const bool userButtonWake = wakeCause == ESP_SLEEP_WAKEUP_EXT1
      && ext1WakeStatusContainsUserButton(ext1WakeStatus, board_.buttons.user);
  if (wakeCause == ESP_SLEEP_WAKEUP_EXT1) {
    // EXT1 leaves an RTC-capable pad under RTC IO control. Restore GPIO4
    // before applying its normal runtime input/pull-up configuration.
    rtc_gpio_deinit(static_cast<gpio_num_t>(board_.buttons.user));
  }
  pinMode(board_.buttons.user, INPUT_PULLUP);
  if (board_.audio.paEnable != kNoPin) {
    pinMode(board_.audio.paEnable, OUTPUT);
    digitalWrite(board_.audio.paEnable, LOW);
  }
  if (userButtonWake) {
    wokeFromUserButton_ = true;
    delay(30);
    const uint32_t pressedAt = millis();
    while (digitalRead(board_.buttons.user) == LOW && millis() - pressedAt < 5000) delay(20);
    forceNetworkRefresh_ = millis() - pressedAt >= 1200;
    delay(30);
  }

  if (Wire.begin(board_.i2c.sda, board_.i2c.scl, board_.i2c.clockHz)) {
    Wire.setTimeOut(kI2cTimeoutMs);
    const bool pmicReady = impl_->power.begin();
    (void)pmicReady;
    shtc3Ready_ = impl_->sensor.begin();
    rtcReady_ = impl_->rtc.begin();
    PP_LOG("[I2C] pmic=%s ready=%d shtc3=%d rtc=%d\n",
           pmicTypeName(impl_->power.type()), pmicReady ? 1 : 0,
           shtc3Ready_ ? 1 : 0, rtcReady_ ? 1 : 0);
  }

  auto beginSd = [this](uint32_t clockHz) {
    impl_->sdSpi.begin(board_.sd.sck, board_.sd.miso, board_.sd.mosi, board_.sd.cs);
    return SD.begin(board_.sd.cs, impl_->sdSpi, clockHz, "/sd", 8, false);
  };
  sdReady_ = beginSd(board_.sdClockHz);
  if (!sdReady_) {
    SD.end();
    impl_->sdSpi.end();
    sdReady_ = beginSd(board_.sdFallbackClockHz);
  }
  if (sdReady_) {
    const char* directories[] = {
      "/originals", "/cache", "/config", "/logs", "/inktime", "/inktime/schedule",
      "/inktime/frames", "/inktime/journal", "/inktime/state",
    };
    for (const char* directory : directories) {
      if (!SD.exists(directory) && !SD.mkdir(directory)) {
        sdReady_ = false;
        lastError_ = "SD-DIRECTORY";
        break;
      }
    }
  }
  if (!sdReady_) {
    SD.end();
    impl_->sdSpi.end();
  }
  if (sdReady_) {
    impl_->ioBuffer = static_cast<uint8_t*>(
      heap_caps_malloc(kIoChunkSize, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)
    );
    if (impl_->ioBuffer == nullptr) {
      cacheStatus_ = CacheStatus::Disabled;
      lastError_ = "SD-BOUNCE-BUFFER";
    } else {
      cacheStatus_ = CacheStatus::Miss;
    }
  }
  PP_LOG("[SD] ready=%d cache=%s\n", sdReady_ ? 1 : 0, cacheStatusName(cacheStatus_));
  // Hardware readiness is fatal for framebuffer work and must take priority
  // over optional SD/I2C diagnostics when setup reports its primary error.
  if (!psramReady_) lastError_ = "DEVICE-PSRAM";
  else if (!flashReady_) lastError_ = "DEVICE-FLASH";
  return hardwareReady_;
}

uint8_t* PhotoPainterSupport::allocateWireBuffer(size_t length) const {
  if (!hardwareReady_ || length == 0) return nullptr;
  return static_cast<uint8_t*>(
    heap_caps_malloc(length, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)
  );
}

bool PhotoPainterSupport::loadCachedFrame(
  uint32_t sourceHash,
  DisplayRotation rotation,
  uint8_t** output,
  const char* sourceSha256
) {
  if (output == nullptr) return false;
  *output = nullptr;
  if (forceNetworkRefresh_ || !sdReady_ || impl_->ioBuffer == nullptr || sourceHash == 0) {
    cacheStatus_ = sdReady_ ? CacheStatus::Miss : CacheStatus::Disabled;
    return false;
  }

  char finalPath[64] = {0};
  char temporaryPath[64] = {0};
  char backupPath[64] = {0};
  if (!makeCachePaths(
        sourceHash, rotation, finalPath, temporaryPath, backupPath, sizeof(finalPath))) {
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  if (!SD.exists(finalPath) && SD.exists(backupPath)) SD.rename(backupPath, finalPath);
  File file = SD.open(finalPath, FILE_READ);
  if (!file) {
    cacheStatus_ = CacheStatus::Miss;
    return false;
  }
  const bool hasFullSource = isSha256Hex(sourceSha256);
  CacheHeader legacyHeader = {};
  CacheHeaderV2 fullHeader = {};
  const bool useFullHeader = hasFullSource
      && static_cast<size_t>(file.size()) == sizeof(CacheHeaderV2) + kPhotoPainterFrameBytes;
  CacheValidation headerValidation = CacheValidation::BadLength;
  if (useFullHeader) {
    const size_t received = file.read(reinterpret_cast<uint8_t*>(&fullHeader), sizeof(fullHeader));
    sdReadBytes_ += static_cast<uint32_t>(received);
    if (received != sizeof(fullHeader)) {
      file.close();
      SD.remove(finalPath);
      cacheStatus_ = CacheStatus::Invalid;
      return false;
    }
  } else if (static_cast<size_t>(file.size()) == sizeof(CacheHeader) + kPhotoPainterFrameBytes) {
    const size_t received = file.read(reinterpret_cast<uint8_t*>(&legacyHeader), sizeof(legacyHeader));
    sdReadBytes_ += static_cast<uint32_t>(received);
    if (received != sizeof(legacyHeader)) {
      file.close();
      SD.remove(finalPath);
      cacheStatus_ = CacheStatus::Invalid;
      return false;
    }
  } else {
    file.close();
    SD.remove(finalPath);
    cacheStatus_ = CacheStatus::Invalid;
    return false;
  }

  uint8_t* framebuffer = allocateWireBuffer(kPhotoPainterFrameBytes);
  if (framebuffer == nullptr) {
    file.close();
    cacheStatus_ = CacheStatus::Error;
    lastError_ = "DEVICE-PSRAM-ALLOC";
    return false;
  }
  size_t total = 0;
  while (total < kPhotoPainterFrameBytes) {
    const size_t requested = min(kIoChunkSize, kPhotoPainterFrameBytes - total);
    const size_t received = file.read(impl_->ioBuffer, requested);
    sdReadBytes_ += static_cast<uint32_t>(received);
    if (received != requested) break;
    memcpy(framebuffer + total, impl_->ioBuffer, received);
    total += received;
  }
  file.close();
  if (total == kPhotoPainterFrameBytes) {
    headerValidation = useFullHeader
      ? validateCacheV2(fullHeader, sourceSha256, rotation, framebuffer, total)
      : validateCache(legacyHeader, sourceHash, rotation, framebuffer, total);
  }
  if (total != kPhotoPainterFrameBytes || headerValidation != CacheValidation::Valid) {
    heap_caps_free(framebuffer);
    SD.remove(finalPath);
    cacheStatus_ = CacheStatus::Invalid;
    return false;
  }
  *output = framebuffer;
  cacheStatus_ = CacheStatus::Hit;
  return true;
}

bool PhotoPainterSupport::loadFormalFrame(
  const char* sourceSha256,
  DisplayRotation rotation,
  uint8_t** output
) {
  if (output == nullptr) return false;
  *output = nullptr;
  if (!sdReady_ || impl_->ioBuffer == nullptr || !isSha256Hex(sourceSha256)) {
    cacheStatus_ = sdReady_ ? CacheStatus::Miss : CacheStatus::Disabled;
    return false;
  }
  char finalPath[128] = {0};
  char temporaryPath[128] = {0};
  char backupPath[128] = {0};
  if (!makeFormalFramePaths(
        sourceSha256, rotation, finalPath, temporaryPath, backupPath, sizeof(finalPath))) {
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  if (!SD.exists(finalPath) && SD.exists(backupPath)) SD.rename(backupPath, finalPath);
  File file = SD.open(finalPath, FILE_READ);
  if (!file || static_cast<size_t>(file.size())
      != sizeof(FormalFrameHeader) + kPhotoPainterFrameBytes) {
    if (file) file.close();
    if (SD.exists(finalPath)) SD.remove(finalPath);
    if (SD.exists(backupPath)) SD.rename(backupPath, finalPath);
    cacheStatus_ = CacheStatus::Miss;
    return false;
  }
  FormalFrameHeader header = {};
  const size_t headerReceived = file.read(reinterpret_cast<uint8_t*>(&header), sizeof(header));
  sdReadBytes_ += static_cast<uint32_t>(headerReceived);
  if (headerReceived != sizeof(header)) {
    file.close();
    SD.remove(finalPath);
    if (SD.exists(backupPath)) SD.rename(backupPath, finalPath);
    cacheStatus_ = CacheStatus::Invalid;
    return false;
  }
  uint8_t* framebuffer = allocateWireBuffer(kPhotoPainterFrameBytes);
  if (framebuffer == nullptr) {
    file.close();
    cacheStatus_ = CacheStatus::Error;
    lastError_ = "DEVICE-PSRAM-ALLOC";
    return false;
  }
  size_t total = 0;
  while (total < kPhotoPainterFrameBytes) {
    const size_t requested = min(kIoChunkSize, kPhotoPainterFrameBytes - total);
    const size_t received = file.read(impl_->ioBuffer, requested);
    sdReadBytes_ += static_cast<uint32_t>(received);
    if (received != requested) break;
    memcpy(framebuffer + total, impl_->ioBuffer, received);
    total += received;
  }
  file.close();
  if (total != kPhotoPainterFrameBytes
      || validateFormalFrameHeader(
           header, sourceSha256, rotation, framebuffer, total) != CacheValidation::Valid) {
    heap_caps_free(framebuffer);
    SD.remove(finalPath);
    if (SD.exists(backupPath)) SD.rename(backupPath, finalPath);
    cacheStatus_ = CacheStatus::Invalid;
    return false;
  }
  *output = framebuffer;
  cacheStatus_ = CacheStatus::Hit;
  return true;
}

bool PhotoPainterSupport::convertFrame(
  const uint8_t* wire,
  size_t wireLength,
  bool indexed4,
  DisplayRotation rotation,
  uint8_t** output
) {
  if (output == nullptr) return false;
  *output = nullptr;
  uint8_t* framebuffer = allocateWireBuffer(kPhotoPainterFrameBytes);
  if (framebuffer == nullptr) {
    lastError_ = "DEVICE-PSRAM-ALLOC";
    return false;
  }
  bool invalidLogicalPalette = false;
  if (!convertWireFrameToNative(
        wire, wireLength, indexed4, rotation, framebuffer, kPhotoPainterFrameBytes,
        &invalidLogicalPalette)) {
    heap_caps_free(framebuffer);
    lastError_ = invalidLogicalPalette
      ? "FRAME_INVALID_PALETTE_INDEX"
      : "DEVICE-FRAME-CONVERT";
    return false;
  }
  *output = framebuffer;
  return true;
}

bool PhotoPainterSupport::convertAndCache(
  const uint8_t* wire,
  size_t wireLength,
  bool indexed4,
  uint32_t sourceHash,
  DisplayRotation rotation,
  uint8_t** output,
  const char* sourceSha256
) {
  if (!convertFrame(wire, wireLength, indexed4, rotation, output)) return false;
  uint8_t* framebuffer = *output;

  if (!sdReady_ || impl_->ioBuffer == nullptr || sourceHash == 0) {
    cacheStatus_ = CacheStatus::Disabled;
    return true;
  }
  char finalPath[64] = {0};
  char temporaryPath[64] = {0};
  char backupPath[64] = {0};
  if (!makeCachePaths(
        sourceHash, rotation, finalPath, temporaryPath, backupPath, sizeof(finalPath))) {
    cacheStatus_ = CacheStatus::Error;
    return true;
  }
  SD.remove(temporaryPath);
  File file = SD.open(temporaryPath, FILE_WRITE);
  if (!file) {
    cacheStatus_ = CacheStatus::Error;
    return true;
  }
  const uint32_t writeStarted = millis();
  const bool useFullHeader = isSha256Hex(sourceSha256);
  CacheHeader legacyHeader = makeCacheHeader(
    sourceHash, rotation, framebuffer, kPhotoPainterFrameBytes);
  CacheHeaderV2 fullHeader = makeCacheHeaderV2(
    sourceSha256, rotation, framebuffer, kPhotoPainterFrameBytes);
  const uint8_t* headerBytes = useFullHeader
    ? reinterpret_cast<const uint8_t*>(&fullHeader)
    : reinterpret_cast<const uint8_t*>(&legacyHeader);
  const size_t headerSize = useFullHeader ? sizeof(fullHeader) : sizeof(legacyHeader);
  const size_t headerWritten = file.write(headerBytes, headerSize);
  sdWriteBytes_ += static_cast<uint32_t>(headerWritten);
  bool writeOk = headerWritten == headerSize;
  size_t total = 0;
  while (writeOk && total < kPhotoPainterFrameBytes) {
    const size_t requested = min(kIoChunkSize, kPhotoPainterFrameBytes - total);
    memcpy(impl_->ioBuffer, framebuffer + total, requested);
    const size_t written = file.write(impl_->ioBuffer, requested);
    sdWriteBytes_ += static_cast<uint32_t>(written);
    writeOk = written == requested;
    total += written;
  }
  file.flush();
  file.close();
  sdWriteDurationMs_ += millis() - writeStarted;
  if (!writeOk || total != kPhotoPainterFrameBytes) {
    SD.remove(temporaryPath);
    cacheStatus_ = CacheStatus::Error;
    return true;
  }

  SD.remove(backupPath);
  bool movedOld = !SD.exists(finalPath) || SD.rename(finalPath, backupPath);
  bool installed = movedOld && SD.rename(temporaryPath, finalPath);
  if (!installed) {
    SD.remove(temporaryPath);
    if (!SD.exists(finalPath) && SD.exists(backupPath)) SD.rename(backupPath, finalPath);
    cacheStatus_ = CacheStatus::Error;
    return true;
  }
  SD.remove(backupPath);
  cacheStatus_ = CacheStatus::Written;
  return true;
}

bool PhotoPainterSupport::writeFormalFrame(
  const char* sourceSha256,
  DisplayRotation rotation,
  const uint8_t* framebuffer,
  size_t length
) {
  if (!sdReady_ || impl_->ioBuffer == nullptr || !isSha256Hex(sourceSha256)
      || framebuffer == nullptr || length != kPhotoPainterFrameBytes) {
    cacheStatus_ = sdReady_ ? CacheStatus::Error : CacheStatus::Disabled;
    return false;
  }
  struct FormalFrameInFlightGuard final {
    String& value;
    explicit FormalFrameInFlightGuard(String& target, const char* source)
        : value(target) { value = source; }
    ~FormalFrameInFlightGuard() { value = ""; }
  } inFlight(formalFrameInFlightSha256_, sourceSha256);
  char finalPath[128] = {0};
  char temporaryPath[128] = {0};
  char backupPath[128] = {0};
  if (!makeFormalFramePaths(
        sourceSha256, rotation, finalPath, temporaryPath, backupPath, sizeof(finalPath))) {
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  SD.remove(temporaryPath);
  File file = SD.open(temporaryPath, FILE_WRITE);
  if (!file) {
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  const uint32_t writeStarted = millis();
  const FormalFrameHeader header = makeFormalFrameHeader(
    sourceSha256, rotation, framebuffer, length);
  const size_t headerWritten = file.write(
    reinterpret_cast<const uint8_t*>(&header), sizeof(header));
  sdWriteBytes_ += static_cast<uint32_t>(headerWritten);
  bool writeOk = headerWritten == sizeof(header);
  size_t total = 0;
  while (writeOk && total < length) {
    const size_t requested = min(kIoChunkSize, length - total);
    memcpy(impl_->ioBuffer, framebuffer + total, requested);
    const size_t written = file.write(impl_->ioBuffer, requested);
    sdWriteBytes_ += static_cast<uint32_t>(written);
    writeOk = written == requested;
    total += written;
  }
  file.flush();
  file.close();
  sdWriteDurationMs_ += millis() - writeStarted;
  if (!writeOk || total != length) {
    SD.remove(temporaryPath);
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  SD.remove(backupPath);
  const bool movedOld = !SD.exists(finalPath) || SD.rename(finalPath, backupPath);
  const bool installed = movedOld && SD.rename(temporaryPath, finalPath);
  if (!installed) {
    SD.remove(temporaryPath);
    if (!SD.exists(finalPath) && SD.exists(backupPath)) SD.rename(backupPath, finalPath);
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  SD.remove(backupPath);
  cacheStatus_ = CacheStatus::Written;
  return true;
}

namespace {

constexpr uint64_t kFormalFrameFreeSpaceFloorBytes = 8ULL * 1024ULL * 1024ULL;
constexpr size_t kFormalFrameMaximumFiles = 24U;
constexpr uint8_t kFormalFrameGcMaxDeletesPerWake = 4U;
constexpr uint8_t kFormalFrameGcMaxScansPerWake = 32U;
constexpr size_t kFormalFrameReferenceLimit = 64U;

class ProtectedFormalFrames final {
 public:
  void add(const char* sourceSha256) {
    if (!isSha256Hex(sourceSha256)) return;
    for (size_t index = 0; index < count_; ++index) {
      if (values_[index].equalsIgnoreCase(sourceSha256)) return;
    }
    if (count_ < kFormalFrameReferenceLimit) values_[count_++] = sourceSha256;
  }

  bool contains(const String& sourceSha256) const {
    for (size_t index = 0; index < count_; ++index) {
      if (values_[index].equalsIgnoreCase(sourceSha256)) return true;
    }
    return false;
  }

 private:
  String values_[kFormalFrameReferenceLimit];
  size_t count_ = 0;
};

bool addScheduleFrameReferences(
  const char* scheduleJson,
  ProtectedFormalFrames& protectedFrames
) {
  if (scheduleJson == nullptr || scheduleJson[0] == '\0') return true;
  JsonDocument document;
  if (deserializeJson(document, scheduleJson) || document.overflowed()) return false;
  const JsonVariantConst rawSlots = document["slots"];
  if (!rawSlots.is<JsonArrayConst>()) return false;
  const JsonArrayConst slots = rawSlots.as<JsonArrayConst>();
  if (slots.size() > kFormalFrameReferenceLimit) return false;
  for (size_t index = 0; index < slots.size(); ++index) {
    const JsonVariantConst rawSlot = slots[index];
    if (!rawSlot.is<JsonObjectConst>()) continue;
    const String sourceSha256 = rawSlot["sha256"] | "";
    protectedFrames.add(sourceSha256.c_str());
  }
  return true;
}

bool scheduleJsonSafeForGc(
  const char* scheduleJson,
  const char* finalPath,
  const char* backupPath
) {
  if (!SD.exists(finalPath) && !SD.exists(backupPath)) return true;
  if (scheduleJson == nullptr || scheduleJson[0] == '\0') return false;
  JsonDocument document;
  if (deserializeJson(document, scheduleJson) || document.overflowed()) return false;
  return document["slots"].is<JsonArrayConst>();
}

bool formalFrameShaFromPath(const String& path, String& sourceSha256) {
  const int slash = path.lastIndexOf('/');
  const String filename = slash >= 0 ? path.substring(slash + 1) : path;
  if (filename.length() != 71U && filename.length() != 73U) return false;
  if (!filename.endsWith(".itf")) return false;
  const String suffix = filename.substring(64U);
  if (suffix != "-r0.itf" && suffix != "-r180.itf") return false;
  sourceSha256 = filename.substring(0, 64U);
  return isSha256Hex(sourceSha256.c_str());
}

size_t countFormalFrameFiles() {
  File directory = SD.open("/inktime/frames");
  if (!directory || !directory.isDirectory()) {
    if (directory) directory.close();
    return 0U;
  }
  size_t count = 0U;
  size_t scanned = 0U;
  File file = directory.openNextFile();
  while (file && count <= kFormalFrameMaximumFiles
      && scanned < kFormalFrameGcMaxScansPerWake) {
    ++scanned;
    String sourceSha256;
    if (!file.isDirectory() && formalFrameShaFromPath(file.name(), sourceSha256)) ++count;
    file.close();
    file = directory.openNextFile();
  }
  directory.close();
  return count;
}

}  // namespace

bool PhotoPainterSupport::runFormalFrameGc(
  const char* activeScheduleJson,
  const char* stagedNextScheduleJson,
  const char* currentFrameSha256,
  const char* lastGoodFrameSha256,
  const char* inFlightFrameSha256,
  const char* recoveryFrameSha256
) {
  if (!sdReady_) return false;
  if (!scheduleJsonSafeForGc(
        activeScheduleJson, "/inktime/schedule/active.json", "/inktime/schedule/active.bak")
      || !scheduleJsonSafeForGc(
        stagedNextScheduleJson,
        "/inktime/schedule/staged_next.json",
        "/inktime/schedule/staged_next.bak")) {
    return false;
  }

  ProtectedFormalFrames protectedFrames;
  if (!addScheduleFrameReferences(activeScheduleJson, protectedFrames)
      || !addScheduleFrameReferences(stagedNextScheduleJson, protectedFrames)) {
    return false;
  }
  protectedFrames.add(currentFrameSha256);
  protectedFrames.add(lastGoodFrameSha256);
  protectedFrames.add(inFlightFrameSha256);
  protectedFrames.add(recoveryFrameSha256);
  // The internal guard covers a formal write that is in progress even if the
  // caller did not pass the optional in-flight reference explicitly.
  protectedFrames.add(formalFrameInFlightSha256_.c_str());

  const size_t formalFrameFiles = countFormalFrameFiles();
  const uint64_t totalBytes = SD.totalBytes();
  const uint64_t usedBytes = SD.usedBytes();
  const uint64_t freeBytes = totalBytes > usedBytes ? totalBytes - usedBytes : 0U;
  const bool pressure = freeBytes < kFormalFrameFreeSpaceFloorBytes;
  if (!pressure && formalFrameFiles <= kFormalFrameMaximumFiles) return true;

  File directory = SD.open("/inktime/frames");
  if (!directory || !directory.isDirectory()) {
    if (directory) directory.close();
    return false;
  }
  uint8_t deletedThisWake = 0U;
  uint8_t scannedThisWake = 0U;
  File file = directory.openNextFile();
  while (file && deletedThisWake < kFormalFrameGcMaxDeletesPerWake
      && scannedThisWake < kFormalFrameGcMaxScansPerWake) {
    ++scannedThisWake;
    String sourceSha256;
    const String entryName = file.name();
    const String path = entryName.startsWith("/")
      ? entryName
      : String("/inktime/frames/") + entryName;
    const uint32_t fileBytes = static_cast<uint32_t>(file.size());
    const bool candidate = !file.isDirectory()
      && formalFrameShaFromPath(path, sourceSha256);
    const bool protectedReference = candidate && protectedFrames.contains(sourceSha256);
    file.close();
    if (protectedReference) {
      if (gcSkippedProtected_ < UINT32_MAX) ++gcSkippedProtected_;
    } else if (candidate && SD.remove(path.c_str())) {
      if (gcDeletedFiles_ < UINT32_MAX) ++gcDeletedFiles_;
      if (UINT32_MAX - gcDeletedBytes_ < fileBytes) gcDeletedBytes_ = UINT32_MAX;
      else gcDeletedBytes_ += fileBytes;
      ++deletedThisWake;
    }
    file = directory.openNextFile();
  }
  directory.close();
  return true;
}

bool PhotoPainterSupport::writeActiveSchedule(const char* json, size_t length) {
  static constexpr size_t kMaxScheduleBytes = 32768U;
  if (!sdReady_ || json == nullptr || length == 0 || length > kMaxScheduleBytes) {
    cacheStatus_ = sdReady_ ? CacheStatus::Error : CacheStatus::Disabled;
    return false;
  }
  char finalPath[64] = {0};
  char temporaryPath[64] = {0};
  char backupPath[64] = {0};
  if (!makeActiveSchedulePaths(
        finalPath, temporaryPath, backupPath, sizeof(finalPath))) {
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  SD.remove(temporaryPath);
  File file = SD.open(temporaryPath, FILE_WRITE);
  if (!file) {
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  const uint32_t writeStarted = millis();
  const size_t written = file.write(
    reinterpret_cast<const uint8_t*>(json), length);
  sdWriteBytes_ += static_cast<uint32_t>(written);
  file.flush();
  file.close();
  sdWriteDurationMs_ += millis() - writeStarted;
  if (written != length) {
    SD.remove(temporaryPath);
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  SD.remove(backupPath);
  const bool movedOld = !SD.exists(finalPath) || SD.rename(finalPath, backupPath);
  const bool installed = movedOld && SD.rename(temporaryPath, finalPath);
  if (!installed) {
    SD.remove(temporaryPath);
    if (!SD.exists(finalPath) && SD.exists(backupPath)) SD.rename(backupPath, finalPath);
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  SD.remove(backupPath);
  cacheStatus_ = CacheStatus::Written;
  return true;
}

bool PhotoPainterSupport::readActiveSchedule(String& json) {
  static constexpr size_t kMaxScheduleBytes = 32768U;
  json = "";
  if (!sdReady_) {
    cacheStatus_ = CacheStatus::Disabled;
    return false;
  }
  char finalPath[64] = {0};
  char temporaryPath[64] = {0};
  char backupPath[64] = {0};
  if (!makeActiveSchedulePaths(
        finalPath, temporaryPath, backupPath, sizeof(finalPath))) {
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  if (!SD.exists(finalPath) && SD.exists(backupPath)) SD.rename(backupPath, finalPath);
  File file = SD.open(finalPath, FILE_READ);
  if (!file || file.size() <= 0 || static_cast<size_t>(file.size()) > kMaxScheduleBytes) {
    if (file) file.close();
    cacheStatus_ = CacheStatus::Miss;
    return false;
  }
  const size_t length = static_cast<size_t>(file.size());
  json.reserve(length + 1U);
  while (file.available()) {
    const int value = file.read();
    if (value >= 0) {
      ++sdReadBytes_;
      json += static_cast<char>(value);
    }
  }
  file.close();
  if (json.length() != length) {
    json = "";
    cacheStatus_ = CacheStatus::Invalid;
    return false;
  }
  cacheStatus_ = CacheStatus::Hit;
  return true;
}

namespace {

String scheduleIdFromJson(const String& json) {
  if (json.length() == 0U) return "";
  JsonDocument document;
  if (deserializeJson(document, json) || document.overflowed()) return "";
  const JsonVariantConst rawScheduleId = document["schedule_id"];
  if (!rawScheduleId.is<const char*>()) return "";
  const String scheduleId = rawScheduleId.as<const char*>();
  if (scheduleId.length() == 0U || scheduleId.length() > 128U) return "";
  return scheduleId;
}

}  // namespace

String PhotoPainterSupport::activeScheduleId() {
  String json;
  return readActiveSchedule(json) ? scheduleIdFromJson(json) : String("");
}

bool PhotoPainterSupport::writeStagedNextSchedule(const char* json, size_t length) {
  static constexpr size_t kMaxScheduleBytes = 32768U;
  if (!sdReady_ || json == nullptr || length == 0 || length > kMaxScheduleBytes) {
    cacheStatus_ = sdReady_ ? CacheStatus::Error : CacheStatus::Disabled;
    return false;
  }
  char finalPath[64] = {0};
  char temporaryPath[64] = {0};
  char backupPath[64] = {0};
  if (!makeStagedNextSchedulePaths(
        finalPath, temporaryPath, backupPath, sizeof(finalPath))) {
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  SD.remove(temporaryPath);
  File file = SD.open(temporaryPath, FILE_WRITE);
  if (!file) {
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  const uint32_t writeStarted = millis();
  const size_t written = file.write(reinterpret_cast<const uint8_t*>(json), length);
  sdWriteBytes_ += static_cast<uint32_t>(written);
  file.flush();
  file.close();
  sdWriteDurationMs_ += millis() - writeStarted;
  if (written != length) {
    SD.remove(temporaryPath);
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  SD.remove(backupPath);
  const bool movedOld = !SD.exists(finalPath) || SD.rename(finalPath, backupPath);
  const bool installed = movedOld && SD.rename(temporaryPath, finalPath);
  if (!installed) {
    SD.remove(temporaryPath);
    if (!SD.exists(finalPath) && SD.exists(backupPath)) SD.rename(backupPath, finalPath);
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  SD.remove(backupPath);
  cacheStatus_ = CacheStatus::Written;
  return true;
}

bool PhotoPainterSupport::readStagedNextSchedule(String& json) {
  static constexpr size_t kMaxScheduleBytes = 32768U;
  json = "";
  if (!sdReady_) {
    cacheStatus_ = CacheStatus::Disabled;
    return false;
  }
  char finalPath[64] = {0};
  char temporaryPath[64] = {0};
  char backupPath[64] = {0};
  if (!makeStagedNextSchedulePaths(
        finalPath, temporaryPath, backupPath, sizeof(finalPath))) {
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  if (!SD.exists(finalPath) && SD.exists(backupPath)) SD.rename(backupPath, finalPath);
  File file = SD.open(finalPath, FILE_READ);
  if (!file || file.size() <= 0 || static_cast<size_t>(file.size()) > kMaxScheduleBytes) {
    if (file) file.close();
    cacheStatus_ = CacheStatus::Miss;
    return false;
  }
  const size_t length = static_cast<size_t>(file.size());
  json.reserve(length + 1U);
  while (file.available()) {
    const int value = file.read();
    if (value >= 0) {
      ++sdReadBytes_;
      json += static_cast<char>(value);
    }
  }
  file.close();
  if (json.length() != length) {
    json = "";
    cacheStatus_ = CacheStatus::Invalid;
    return false;
  }
  cacheStatus_ = CacheStatus::Hit;
  return true;
}

String PhotoPainterSupport::stagedNextScheduleId() {
  String json;
  return readStagedNextSchedule(json) ? scheduleIdFromJson(json) : String("");
}

bool PhotoPainterSupport::clearStagedNextSchedule() {
  if (!sdReady_) {
    cacheStatus_ = CacheStatus::Disabled;
    return false;
  }
  char finalPath[64] = {0};
  char temporaryPath[64] = {0};
  char backupPath[64] = {0};
  if (!makeStagedNextSchedulePaths(
        finalPath, temporaryPath, backupPath, sizeof(finalPath))) {
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  SD.remove(finalPath);
  SD.remove(temporaryPath);
  SD.remove(backupPath);
  cacheStatus_ = CacheStatus::Written;
  return true;
}

bool PhotoPainterSupport::promoteStagedNextSchedule() {
  if (!sdReady_) {
    cacheStatus_ = CacheStatus::Disabled;
    return false;
  }
  char activeFinal[64] = {0};
  char activeTemporary[64] = {0};
  char activeBackup[64] = {0};
  char stagedFinal[64] = {0};
  char stagedTemporary[64] = {0};
  char stagedBackup[64] = {0};
  if (!makeActiveSchedulePaths(
        activeFinal, activeTemporary, activeBackup, sizeof(activeFinal))
      || !makeStagedNextSchedulePaths(
        stagedFinal, stagedTemporary, stagedBackup, sizeof(stagedFinal))) {
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  if (!SD.exists(stagedFinal) && SD.exists(stagedBackup)) SD.rename(stagedBackup, stagedFinal);
  if (!SD.exists(stagedFinal)) {
    cacheStatus_ = CacheStatus::Miss;
    return false;
  }
  SD.remove(activeBackup);
  const bool movedActive = !SD.exists(activeFinal) || SD.rename(activeFinal, activeBackup);
  const bool installed = movedActive && SD.rename(stagedFinal, activeFinal);
  if (!installed) {
    if (!SD.exists(activeFinal) && SD.exists(activeBackup)) SD.rename(activeBackup, activeFinal);
    cacheStatus_ = CacheStatus::Error;
    return false;
  }
  SD.remove(activeTemporary);
  SD.remove(activeBackup);
  SD.remove(stagedTemporary);
  SD.remove(stagedBackup);
  cacheStatus_ = CacheStatus::Written;
  return true;
}

bool PhotoPainterSupport::displayFrame(const uint8_t* framebuffer, size_t length) {
  if (!hardwareReady_ || impl_ == nullptr || framebuffer == nullptr
      || length != kPhotoPainterFrameBytes) {
    lastError_ = "DEVICE-FRAMEBUFFER";
    return false;
  }
  if (!impl_->display.begin() || !impl_->display.displayFrame(framebuffer, length)) {
    lastError_ = impl_->display.lastError();
    return false;
  }
  lastRefreshDurationMs_ = impl_->display.lastRefreshDurationMs();
  return true;
}

bool PhotoPainterSupport::displayPairingScreen(
    const char* ssid,
    const char* password,
    const char* setup_url,
    const char* pairing_code
) {
  if (!hardwareReady_ || impl_ == nullptr) {
    lastError_ = "DEVICE-PAIRING-DISPLAY";
    return false;
  }
  uint8_t* frame = allocateWireBuffer(kPhotoPainterFrameBytes);
  if (frame == nullptr) {
    lastError_ = "DEVICE-PAIRING-MEMORY";
    return false;
  }
  memset(frame, 0x11, kPhotoPainterFrameBytes);
  const uint8_t scale = 3;
  drawPairingText(frame, kPhotoPainterFrameBytes, 30, 42, "INKTIME PAIRING", scale);
  drawPairingText(frame, kPhotoPainterFrameBytes, 30, 112, String("WIFI SSID: ") + String(ssid == nullptr ? "" : ssid), scale);
  drawPairingText(frame, kPhotoPainterFrameBytes, 30, 182, String("AP PASSWORD: ") + String(password == nullptr ? "" : password), scale);
  drawPairingText(frame, kPhotoPainterFrameBytes, 30, 252, String("SETUP: ") + String(setup_url == nullptr ? "" : setup_url), scale);
  drawPairingText(frame, kPhotoPainterFrameBytes, 30, 322, String("CODE: ") + String(pairing_code == nullptr ? "" : pairing_code), scale);
  drawPairingText(frame, kPhotoPainterFrameBytes, 30, 392, "VALID 5 MIN", scale);
  const bool displayed = displayFrame(frame, kPhotoPainterFrameBytes);
  heap_caps_free(frame);
  return displayed;
}

bool PhotoPainterSupport::writeRtc(time_t epoch) {
  return impl_ != nullptr && rtcReady_ && impl_->rtc.writeEpoch(epoch);
}

bool PhotoPainterSupport::readRtc(time_t& epoch) {
  return impl_ != nullptr && rtcReady_ && impl_->rtc.readEpoch(epoch);
}

uint32_t PhotoPainterSupport::i2cRetryCount() const {
  return impl_ == nullptr ? 0U : impl_->i2cTelemetry.retry_count;
}

uint32_t PhotoPainterSupport::i2cBusResetCount() const {
  return impl_ == nullptr ? 0U : impl_->i2cTelemetry.bus_reset_count;
}

uint32_t PhotoPainterSupport::i2cFailClosedCount() const {
  return impl_ == nullptr ? 0U : impl_->i2cTelemetry.fail_closed_count;
}

void PhotoPainterSupport::refreshPowerState() {
  if (impl_ != nullptr) impl_->power.refreshMeasurements();
}

void PhotoPainterSupport::readEnvironment() {
  environmentValid_ = false;
  if (impl_ == nullptr) return;
  refreshPowerState();
  if (shtc3Ready_) {
    environmentValid_ = impl_->sensor.read(temperatureC_, humidityPercent_);
  }
}

bool PhotoPainterSupport::usbConnected() const {
  return impl_ != nullptr && impl_->power.isUsbConnected();
}

PmicType PhotoPainterSupport::pmicType() const {
  return impl_ == nullptr ? PmicType::None : impl_->power.type();
}

float PhotoPainterSupport::batteryVoltage() const {
  return impl_ == nullptr ? 0.0f : impl_->power.batteryVoltage();
}

int PhotoPainterSupport::batteryPercent() const {
  return impl_ == nullptr ? -1 : impl_->power.batteryPercent();
}

void PhotoPainterSupport::prepareForDeepSleep() {
  if (impl_ == nullptr) return;
  impl_->display.safeShutdown();
  if (sdReady_) {
    SD.end();
    impl_->sdSpi.end();
  }
  if (board_.audio.paEnable != kNoPin) {
    pinMode(board_.audio.paEnable, OUTPUT);
    digitalWrite(board_.audio.paEnable, LOW);
  }
  impl_->power.prepareForDeepSleep();
  Wire.end();
}

void PhotoPainterSupport::enableWakeSources() {
  if (board_.buttons.user == kNoPin || !board_.buttons.userActiveLow) return;
  const uint32_t releaseStarted = millis();
  while (digitalRead(board_.buttons.user) == LOW && millis() - releaseStarted < 2000) delay(20);
  if (digitalRead(board_.buttons.user) == LOW) return;
  pinMode(board_.buttons.user, INPUT_PULLUP);
  esp_sleep_enable_ext1_wakeup_io(
    gpioWakeMask(board_.buttons.user),
    ESP_EXT1_WAKEUP_ANY_LOW
  );
}

}  // namespace inktime

#endif
