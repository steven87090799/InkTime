#include "photopainter_support.h"

#if INKTIME_PHOTOPAINTER_ENABLED

#include <Arduino.h>
#include <FS.h>
#include <SD.h>
#include <SPI.h>
#include <Wire.h>
#include <esp_heap_caps.h>
#include <esp_sleep.h>
#include <new>
#include <sys/time.h>

#include "photopainter_core.h"
#include "power_manager.h"
#include "spectra6_73.h"

#ifndef INKTIME_DEBUG_LOG
#define INKTIME_DEBUG_LOG 0
#endif

#ifndef INKTIME_MIN_REFRESH_MV
#define INKTIME_MIN_REFRESH_MV 3500
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

#if INKTIME_DEBUG_LOG
#define PP_LOG(...) DebugSerial.printf(__VA_ARGS__)
#else
#define PP_LOG(...) do { } while (0)
#endif

bool i2cWriteCommand(TwoWire& wire, uint8_t address, uint16_t command) {
  wire.beginTransmission(address);
  wire.write(static_cast<uint8_t>(command >> 8U));
  wire.write(static_cast<uint8_t>(command & 0xFFU));
  return wire.endTransmission() == 0;
}

bool i2cReadRegister(
  TwoWire& wire,
  uint8_t address,
  uint8_t reg,
  uint8_t* data,
  size_t length
) {
  if (data == nullptr || length == 0 || length > 32) return false;
  wire.beginTransmission(address);
  wire.write(reg);
  if (wire.endTransmission(false) != 0) return false;
  const size_t received = wire.requestFrom(address, static_cast<uint8_t>(length), true);
  if (received != length) return false;
  for (size_t index = 0; index < length; ++index) {
    if (!wire.available()) return false;
    data[index] = static_cast<uint8_t>(wire.read());
  }
  return true;
}

bool i2cWriteRegisters(
  TwoWire& wire,
  uint8_t address,
  uint8_t firstRegister,
  const uint8_t* data,
  size_t length
) {
  if (data == nullptr || length == 0 || length > 24) return false;
  wire.beginTransmission(address);
  wire.write(firstRegister);
  wire.write(data, length);
  return wire.endTransmission() == 0;
}

bool i2cProbe(TwoWire& wire, uint8_t address) {
  wire.beginTransmission(address);
  return wire.endTransmission() == 0;
}

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
  explicit ProbePowerManager(TwoWire& wire) : wire_(wire) {}

  bool begin() override {
    type_ = PmicType::None;
    powerSourceKnown_ = false;
    if (!i2cProbe(wire_, kAxp2101Address)) return false;
    uint8_t chipId = 0;
    if (!i2cReadRegister(wire_, kAxp2101Address, kAxp2101ChipIdRegister, &chipId, 1)) {
      type_ = PmicType::Unknown;
      return false;
    }
    if (chipId != kAxp2101ChipId) {
      type_ = PmicType::Unknown;
      return false;
    }
    type_ = PmicType::AXP2101;
    powerSourceKnown_ = true;
    refreshMeasurements();
    return true;
  }

  void refreshMeasurements() override {
    usbConnected_ = false;
    batteryMillivolts_ = 0;
    batteryPercent_ = -1;
    if (type_ != PmicType::AXP2101) return;
    uint8_t status[2] = {0, 0};
    if (!i2cReadRegister(wire_, kAxp2101Address, kAxp2101Status1, status, 2)) return;
    const bool batteryConnected = (status[0] & (1U << 3U)) != 0;
    const bool vbusGood = (status[0] & (1U << 5U)) != 0;
    const bool vbusOverVoltage = (status[1] & (1U << 3U)) != 0;
    usbConnected_ = vbusGood && !vbusOverVoltage;
    if (!batteryConnected) return;
    uint8_t voltage[2] = {0, 0};
    if (i2cReadRegister(
          wire_, kAxp2101Address, kAxp2101BatteryVoltageHigh, voltage, 2)) {
      batteryMillivolts_ = static_cast<uint16_t>((voltage[0] & 0x1FU) << 8U)
                         | voltage[1];
    }
    uint8_t percent = 0;
    if (i2cReadRegister(wire_, kAxp2101Address, kAxp2101BatteryPercent, &percent, 1)
        && percent <= 100) {
      batteryPercent_ = percent;
    }
  }

  PmicType type() const override { return type_; }
  bool isUsbConnected() const override { return usbConnected_; }
  bool isPowerSourceKnown() const override { return powerSourceKnown_; }
  float batteryVoltage() const override { return batteryMillivolts_ / 1000.0f; }
  int batteryPercent() const override { return batteryPercent_; }
  bool allowDisplayRefresh(uint16_t minimumMillivolts) const override {
    if (usbConnected_) return true;
    if (type_ == PmicType::AXP2101) return batteryMillivolts_ >= minimumMillivolts;
    return true;
  }
  void prepareForDeepSleep() override {
    // Deliberately read-only: board revisions must be identified before any
    // PMIC rail voltage or shutdown-register writes are enabled.
  }

 private:
  TwoWire& wire_;
  PmicType type_ = PmicType::None;
  bool powerSourceKnown_ = false;
  bool usbConnected_ = false;
  uint16_t batteryMillivolts_ = 0;
  int batteryPercent_ = -1;
};

class Shtc3Adapter {
 public:
  explicit Shtc3Adapter(TwoWire& wire) : wire_(wire) {}

  bool begin() {
    ready_ = false;
    if (!i2cProbe(wire_, kShtc3Address) || !i2cWriteCommand(wire_, kShtc3Address, 0x3517)) {
      return false;
    }
    delay(1);
    if (!i2cWriteCommand(wire_, kShtc3Address, 0xEFC8)) {
      sleep();
      return false;
    }
    delay(2);
    const size_t received = wire_.requestFrom(kShtc3Address, static_cast<uint8_t>(3), true);
    uint8_t id[3] = {0, 0, 0};
    if (received != 3) {
      sleep();
      return false;
    }
    for (uint8_t index = 0; index < 3; ++index) id[index] = wire_.read();
    ready_ = shtc3Crc8(id, 2) == id[2]
          && ((static_cast<uint16_t>(id[0]) << 8U | id[1]) & 0x083FU) == 0x0807U;
    sleep();
    return ready_;
  }

  bool read(float& temperatureC, float& humidityPercent) {
    if (!ready_ || !i2cWriteCommand(wire_, kShtc3Address, 0x3517)) return false;
    delay(1);
    if (!i2cWriteCommand(wire_, kShtc3Address, 0x7CA2)) {
      sleep();
      return false;
    }
    const uint32_t started = millis();
    while (millis() - started < 15) delay(1);
    const size_t received = wire_.requestFrom(kShtc3Address, static_cast<uint8_t>(6), true);
    uint8_t bytes[6] = {0, 0, 0, 0, 0, 0};
    if (received != 6) {
      sleep();
      return false;
    }
    for (uint8_t index = 0; index < 6; ++index) bytes[index] = wire_.read();
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
  void sleep() { i2cWriteCommand(wire_, kShtc3Address, 0xB098); }
  TwoWire& wire_;
  bool ready_ = false;
};

class Pcf85063Adapter {
 public:
  explicit Pcf85063Adapter(TwoWire& wire) : wire_(wire) {}

  bool begin() {
    ready_ = i2cProbe(wire_, kPcf85063Address);
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
    return i2cWriteRegisters(wire_, kPcf85063Address, 0x04, registers, sizeof(registers));
  }

  bool readEpoch(time_t& epoch) {
    epoch = 0;
    if (!ready_) return false;
    uint8_t registers[7] = {0};
    if (!i2cReadRegister(wire_, kPcf85063Address, 0x04, registers, sizeof(registers))
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
  TwoWire& wire_;
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

struct PhotoPainterSupport::Impl {
  explicit Impl(const BoardConfig& board)
      : epdSpi(FSPI),
        sdSpi(HSPI),
        display(epdSpi, board),
        power(Wire),
        sensor(Wire),
        rtc(Wire) {}

  SPIClass epdSpi;
  SPIClass sdSpi;
  Spectra6_73 display;
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

  pinMode(board_.buttons.user, INPUT_PULLUP);
  if (board_.audio.paEnable != kNoPin) {
    pinMode(board_.audio.paEnable, OUTPUT);
    digitalWrite(board_.audio.paEnable, LOW);
  }
  if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT0) {
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
    const char* directories[] = {"/originals", "/cache", "/config", "/logs"};
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
  uint8_t** output
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
  CacheHeader header = {};
  const bool headerRead = file.read(reinterpret_cast<uint8_t*>(&header), sizeof(header))
                       == sizeof(header);
  const bool plausible = headerRead && header.magic == kCacheMagic
      && header.version == kCacheVersion
      && header.width == board_.display.width && header.height == board_.display.height
      && header.bitsPerPixel == kNativeBitsPerPixel
      && header.rotation == static_cast<uint8_t>(rotation)
      && header.sourceHash == sourceHash && header.payloadSize == kPhotoPainterFrameBytes
      && static_cast<size_t>(file.size()) == sizeof(CacheHeader) + kPhotoPainterFrameBytes;
  if (!plausible) {
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
    if (received != requested) break;
    memcpy(framebuffer + total, impl_->ioBuffer, received);
    total += received;
  }
  file.close();
  if (total != kPhotoPainterFrameBytes
      || validateCache(header, sourceHash, rotation, framebuffer, total) != CacheValidation::Valid) {
    heap_caps_free(framebuffer);
    SD.remove(finalPath);
    cacheStatus_ = CacheStatus::Invalid;
    return false;
  }
  *output = framebuffer;
  cacheStatus_ = CacheStatus::Hit;
  return true;
}

bool PhotoPainterSupport::convertAndCache(
  const uint8_t* wire,
  size_t wireLength,
  bool indexed4,
  uint32_t sourceHash,
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
  if (!convertWireFrameToNative(
        wire, wireLength, indexed4, rotation, framebuffer, kPhotoPainterFrameBytes)) {
    heap_caps_free(framebuffer);
    lastError_ = "DEVICE-FRAME-CONVERT";
    return false;
  }
  *output = framebuffer;

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
  const CacheHeader header = makeCacheHeader(
    sourceHash, rotation, framebuffer, kPhotoPainterFrameBytes);
  bool writeOk = file.write(reinterpret_cast<const uint8_t*>(&header), sizeof(header))
              == sizeof(header);
  size_t total = 0;
  while (writeOk && total < kPhotoPainterFrameBytes) {
    const size_t requested = min(kIoChunkSize, kPhotoPainterFrameBytes - total);
    memcpy(impl_->ioBuffer, framebuffer + total, requested);
    const size_t written = file.write(impl_->ioBuffer, requested);
    writeOk = written == requested;
    total += written;
  }
  file.flush();
  file.close();
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

bool PhotoPainterSupport::displayFrame(const uint8_t* framebuffer, size_t length) {
  if (!hardwareReady_ || impl_ == nullptr || framebuffer == nullptr
      || length != kPhotoPainterFrameBytes) {
    lastError_ = "DEVICE-FRAMEBUFFER";
    return false;
  }
  impl_->power.refreshMeasurements();
  if (!impl_->power.allowDisplayRefresh(INKTIME_MIN_REFRESH_MV)) {
    lastError_ = "DEVICE-LOW-BATTERY";
    return false;
  }
  if (!impl_->display.begin() || !impl_->display.displayFrame(framebuffer, length)) {
    lastError_ = impl_->display.lastError();
    return false;
  }
  lastRefreshDurationMs_ = impl_->display.lastRefreshDurationMs();
  return true;
}

bool PhotoPainterSupport::writeRtc(time_t epoch) {
  return impl_ != nullptr && rtcReady_ && impl_->rtc.writeEpoch(epoch);
}

bool PhotoPainterSupport::readRtc(time_t& epoch) {
  return impl_ != nullptr && rtcReady_ && impl_->rtc.readEpoch(epoch);
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

bool PhotoPainterSupport::powerSourceKnown() const {
  return impl_ != nullptr && impl_->power.isPowerSourceKnown();
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
  if (board_.buttons.user == kNoPin) return;
  const uint32_t releaseStarted = millis();
  while (digitalRead(board_.buttons.user) == LOW && millis() - releaseStarted < 2000) delay(20);
  if (digitalRead(board_.buttons.user) == LOW) return;
  pinMode(board_.buttons.user, INPUT_PULLUP);
  esp_sleep_enable_ext0_wakeup(
    static_cast<gpio_num_t>(board_.buttons.user),
    board_.buttons.userActiveLow ? 0 : 1
  );
}

}  // namespace inktime

#endif
