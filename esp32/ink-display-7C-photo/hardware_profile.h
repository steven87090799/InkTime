#pragma once

#include <stddef.h>
#include <stdint.h>

#if defined(ARDUINO_ARCH_ESP32)
#include <sdkconfig.h>
#endif

#define DEVICE_PROFILE_EXISTING_DEFAULT 1
#define DEVICE_PROFILE_WAVESHARE_PHOTOPAINTER 2

#ifndef DEVICE_PROFILE
#define DEVICE_PROFILE DEVICE_PROFILE_EXISTING_DEFAULT
#endif

namespace inktime {

constexpr int8_t kNoPin = -1;

enum class DeviceProfile : uint8_t {
  ExistingDefault = DEVICE_PROFILE_EXISTING_DEFAULT,
  WavesharePhotoPainter = DEVICE_PROFILE_WAVESHARE_PHOTOPAINTER,
};

enum class DisplayRotation : uint8_t {
  Rotate0 = 0,
  Rotate180 = 180,
};

enum class PmicType : uint8_t {
  None,
  AXP2101,
  TG28,
  Unknown,
};

struct SpiPins {
  int8_t cs;
  int8_t sck;
  int8_t miso;
  int8_t mosi;
};

struct DisplayConfig {
  SpiPins spi;
  int8_t dc;
  int8_t reset;
  int8_t busy;
  uint16_t width;
  uint16_t height;
  uint32_t clockHz;
  bool busyActiveLow;
};

struct I2cConfig {
  int8_t sda;
  int8_t scl;
  uint32_t clockHz;
};

struct ButtonConfig {
  int8_t factoryReset;
  int8_t boot;
  int8_t user;
  int8_t power;
  bool factoryResetActiveLow;
  bool userActiveLow;
};

struct AudioConfig {
  int8_t mclk;
  int8_t ws;
  int8_t bclk;
  int8_t din;
  int8_t dout;
  int8_t paEnable;
  uint32_t sampleRate;
};

struct Capabilities {
  bool hasPsram;
  bool hasSdCard;
  bool hasRtc;
  bool hasShtc3;
  bool hasAudio;
  bool hasPmic;
};

struct BoardConfig {
  const char* name;
  DeviceProfile profile;
  int8_t statusLed;
  DisplayConfig display;
  SpiPins sd;
  I2cConfig i2c;
  ButtonConfig buttons;
  AudioConfig audio;
  Capabilities capabilities;
  uint16_t payloadWidth;
  uint16_t payloadHeight;
  uint32_t sdClockHz;
  uint32_t sdFallbackClockHz;
  uint32_t requiredFlashBytes;
  uint32_t requiredPsramBytes;
};

constexpr SpiPins kNoSpiPins = {kNoPin, kNoPin, kNoPin, kNoPin};
constexpr I2cConfig kNoI2c = {kNoPin, kNoPin, 0};
constexpr AudioConfig kNoAudio = {
  kNoPin, kNoPin, kNoPin, kNoPin, kNoPin, kNoPin, 0,
};

#if DEVICE_PROFILE == DEVICE_PROFILE_EXISTING_DEFAULT

constexpr DeviceProfile kDeviceProfile = DeviceProfile::ExistingDefault;
constexpr BoardConfig kBoardConfig = {
  "inktime-existing-default",
  kDeviceProfile,
  2,
  {{11, 10, kNoPin, 9}, 12, 13, 14, 800, 480, 4000000, true},
  kNoSpiPins,
  kNoI2c,
  {38, kNoPin, 38, kNoPin, true, true},
  kNoAudio,
  {true, false, false, false, false, false},
  480,
  800,
  0,
  0,
  0,
  0,
};

#define INKTIME_PHOTOPAINTER_ENABLED 0

// Preserve the already-supported replacement panel flag on the existing PCB.
#ifndef INKTIME_PANEL_GDEP073E01
#define INKTIME_PANEL_GDEP073E01 0
#endif

#if INKTIME_PANEL_GDEP073E01
#define INKTIME_PANEL_CLASS GxEPD2_730c_GDEP073E01
#define INKTIME_PANEL_PROFILE "gdep073e01_6c"
#else
#define INKTIME_PANEL_CLASS GxEPD2_730c_GDEY073D46
#define INKTIME_PANEL_PROFILE "gdey073d46_7c"
#endif

#elif DEVICE_PROFILE == DEVICE_PROFILE_WAVESHARE_PHOTOPAINTER

constexpr DeviceProfile kDeviceProfile = DeviceProfile::WavesharePhotoPainter;
constexpr BoardConfig kBoardConfig = {
  "waveshare-esp32-s3-photopainter",
  kDeviceProfile,
  kNoPin,
  {{9, 10, kNoPin, 11}, 8, 12, 13, 800, 480, 4000000, true},
  {38, 39, 40, 41},
  {47, 48, 400000},
  {kNoPin, 0, 4, 5, true, true},
  {14, 16, 15, 18, 17, 7, 24000},
  {true, true, true, true, true, true},
  480,
  800,
  20000000,
  4000000,
  16U * 1024U * 1024U,
  8U * 1024U * 1024U,
};

#define INKTIME_PHOTOPAINTER_ENABLED 1
#define INKTIME_PANEL_PROFILE "gdep073e01_6c"

#if defined(ARDUINO_ARCH_ESP32) && !defined(CONFIG_IDF_TARGET_ESP32S3)
#error "Waveshare PhotoPainter requires an ESP32-S3 build target"
#endif
#if defined(ARDUINO_ARCH_ESP32) && !defined(BOARD_HAS_PSRAM)
#error "Waveshare PhotoPainter requires the Arduino OPI PSRAM option"
#endif
#if defined(ARDUINO_ARCH_ESP32) && !defined(CONFIG_SPIRAM_MODE_OCT)
#error "Waveshare PhotoPainter requires OPI (octal) PSRAM"
#endif
#if defined(ARDUINO_ARCH_ESP32) && !defined(CONFIG_ESPTOOLPY_FLASHSIZE_16MB)
#error "Waveshare PhotoPainter requires the 16 MB Flash build option"
#endif

#else
#error "Unsupported DEVICE_PROFILE value"
#endif

constexpr size_t frameBufferBytes(const BoardConfig& board) {
  return static_cast<size_t>(board.display.width) * board.display.height / 2U;
}

constexpr bool samePin(int8_t lhs, int8_t rhs) {
  return lhs != kNoPin && rhs != kNoPin && lhs == rhs;
}

constexpr bool spiPinsOverlap(const SpiPins& lhs, const SpiPins& rhs) {
  return samePin(lhs.cs, rhs.cs) || samePin(lhs.cs, rhs.sck)
      || samePin(lhs.cs, rhs.miso) || samePin(lhs.cs, rhs.mosi)
      || samePin(lhs.sck, rhs.cs) || samePin(lhs.sck, rhs.sck)
      || samePin(lhs.sck, rhs.miso) || samePin(lhs.sck, rhs.mosi)
      || samePin(lhs.miso, rhs.cs) || samePin(lhs.miso, rhs.sck)
      || samePin(lhs.miso, rhs.miso) || samePin(lhs.miso, rhs.mosi)
      || samePin(lhs.mosi, rhs.cs) || samePin(lhs.mosi, rhs.sck)
      || samePin(lhs.mosi, rhs.miso) || samePin(lhs.mosi, rhs.mosi);
}

static_assert(kBoardConfig.display.width == 800, "7.3-inch panel width must be 800");
static_assert(kBoardConfig.display.height == 480, "7.3-inch panel height must be 480");
static_assert(frameBufferBytes(kBoardConfig) == 192000, "4bpp framebuffer must be 192000 bytes");
static_assert(
  kBoardConfig.payloadWidth * kBoardConfig.payloadHeight
      == kBoardConfig.display.width * kBoardConfig.display.height,
  "wire payload and physical panel must contain the same number of pixels"
);

#if INKTIME_PHOTOPAINTER_ENABLED
static_assert(!spiPinsOverlap(kBoardConfig.display.spi, kBoardConfig.sd),
              "PhotoPainter display and SD must use distinct pins and SPI buses");
static_assert(kBoardConfig.buttons.user != kBoardConfig.buttons.power,
              "GPIO4 user input must not alias the reserved GPIO5 power button");
static_assert(kBoardConfig.buttons.factoryReset == kNoPin,
              "GPIO0 must remain reserved for the PhotoPainter BOOT function");
static_assert(kBoardConfig.requiredFlashBytes == 16U * 1024U * 1024U,
              "PhotoPainter profile must require its 16 MB Flash");
static_assert(kBoardConfig.requiredPsramBytes == 8U * 1024U * 1024U,
              "PhotoPainter profile must require its 8 MB OPI PSRAM");
#endif

}  // namespace inktime
