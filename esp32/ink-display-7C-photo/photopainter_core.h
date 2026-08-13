#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "hardware_profile.h"

namespace inktime {

constexpr uint32_t kCacheMagic = 0x49544643UL;  // "ITFC"
constexpr uint16_t kCacheVersion = 1;
constexpr uint32_t kFormalFrameMagic = 0x32544649UL;  // "ITF2"
constexpr uint16_t kFormalFrameVersion = 1;
constexpr uint8_t kNativeBitsPerPixel = 4;
constexpr uint16_t kPhotoPainterWidth = kBoardConfig.display.width;
constexpr uint16_t kPhotoPainterHeight = kBoardConfig.display.height;
constexpr uint16_t kPayloadWidth = kBoardConfig.payloadWidth;
constexpr uint16_t kPayloadHeight = kBoardConfig.payloadHeight;
constexpr size_t kPhotoPainterFrameBytes = frameBufferBytes(kBoardConfig);
constexpr uint8_t kSha256Bytes = 32;

// The server keeps the existing portrait payload contract. PhotoPainter converts
// it once into the panel-native 800x480 row-major buffer before display/cache.
struct PhysicalCoordinate {
  uint16_t x;
  uint16_t y;
};

struct CacheHeader {
  uint32_t magic;
  uint16_t version;
  uint16_t width;
  uint16_t height;
  uint8_t bitsPerPixel;
  uint8_t rotation;
  uint32_t sourceHash;
  uint32_t payloadSize;
  uint32_t payloadCrc32;
};

static_assert(sizeof(CacheHeader) == 24, "CacheHeader layout must remain stable");

// Versioned cache format for the offline schedule path.  The legacy compact
// sourceHash remains readable for old SD cards, but new cache writes may use
// this header so two different full SHA-256 values can never collide.
struct CacheHeaderV2 {
  uint32_t magic;
  uint16_t version;
  uint16_t width;
  uint16_t height;
  uint8_t bitsPerPixel;
  uint8_t rotation;
  uint8_t sourceSha256[kSha256Bytes];
  uint32_t payloadSize;
  uint32_t payloadCrc32;
};

static_assert(sizeof(CacheHeaderV2) == 52, "CacheHeaderV2 layout must remain stable");

#pragma pack(push, 1)
struct FormalFrameHeader {
  uint32_t magic;
  uint16_t version;
  uint16_t width;
  uint16_t height;
  uint8_t bitsPerPixel;
  uint8_t rotation;
  uint16_t reserved;
  uint32_t payloadSize;
  uint32_t payloadCrc32;
  uint8_t sourceSha256[kSha256Bytes];
};
#pragma pack(pop)

static_assert(sizeof(FormalFrameHeader) == 54, "FormalFrameHeader layout must remain stable");

enum class CacheValidation : uint8_t {
  Valid,
  BadMagic,
  BadVersion,
  BadDimensions,
  BadFormat,
  BadRotation,
  BadSource,
  BadLength,
  BadCrc,
};

inline uint32_t crc32(const uint8_t* data, size_t length) {
  if (data == nullptr && length != 0) return 0;
  uint32_t crc = 0xFFFFFFFFUL;
  for (size_t index = 0; index < length; ++index) {
    crc ^= data[index];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc >> 1U) ^ ((crc & 1U) ? 0xEDB88320UL : 0U);
    }
  }
  return ~crc;
}

inline uint8_t shtc3Crc8(const uint8_t* data, size_t length) {
  uint8_t crc = 0xFF;
  for (size_t index = 0; index < length; ++index) {
    crc ^= data[index];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x80U) ? static_cast<uint8_t>((crc << 1U) ^ 0x31U)
                          : static_cast<uint8_t>(crc << 1U);
    }
  }
  return crc;
}

inline uint8_t readPacked4(const uint8_t* data, size_t pixel) {
  const uint8_t packed = data[pixel / 2U];
  return (pixel & 1U) == 0 ? static_cast<uint8_t>(packed >> 4U)
                           : static_cast<uint8_t>(packed & 0x0FU);
}

inline uint8_t readPacked2(const uint8_t* data, size_t pixel) {
  const uint8_t packed = data[pixel / 4U];
  return static_cast<uint8_t>((packed >> (6U - (pixel & 3U) * 2U)) & 0x03U);
}

inline bool validLogicalIndexed4Color(uint8_t color) {
  return color <= 5U;
}

inline bool writePacked4(
  uint8_t* data,
  size_t dataSize,
  uint16_t width,
  uint16_t x,
  uint16_t y,
  uint8_t color
) {
  const size_t byteIndex = static_cast<size_t>(y) * (width / 2U) + x / 2U;
  if (data == nullptr || byteIndex >= dataSize || color > 0x0F) return false;
  if ((x & 1U) == 0) {
    data[byteIndex] = static_cast<uint8_t>((data[byteIndex] & 0x0FU) | (color << 4U));
  } else {
    data[byteIndex] = static_cast<uint8_t>((data[byteIndex] & 0xF0U) | color);
  }
  return true;
}

inline uint8_t nativeColorFromIndexed4(uint8_t color) {
  // InkTime/Gx logical palette -> E6 controller-native palette.
  switch (color) {
    case 0: return 0;  // black
    case 1: return 1;  // white
    case 2: return 6;  // green
    case 3: return 5;  // blue
    case 4: return 3;  // red
    case 5: return 2;  // yellow
    default: return 0xFF; // invalid logical input; callers must fail closed
  }
}

inline uint8_t nativeColorFrom2bpp(uint8_t color) {
  switch (color) {
    case 0: return 0;  // black
    case 1: return 1;  // white
    case 2: return 3;  // red
    case 3: return 2;  // yellow
    default: return 1;
  }
}

inline bool portraitToPhysical(
  uint16_t x,
  uint16_t y,
  DisplayRotation rotation,
  PhysicalCoordinate& output
) {
  if (x >= kPayloadWidth || y >= kPayloadHeight) return false;
  if (rotation == DisplayRotation::Rotate0) {
    output.x = static_cast<uint16_t>(kPhotoPainterWidth - 1U - y);
    output.y = x;
  } else {
    output.x = y;
    output.y = static_cast<uint16_t>(kPhotoPainterHeight - 1U - x);
  }
  return output.x < kPhotoPainterWidth && output.y < kPhotoPainterHeight;
}

inline bool convertWireFrameToNative(
  const uint8_t* wire,
  size_t wireSize,
  bool indexed4,
  DisplayRotation rotation,
  uint8_t* nativeFrame,
  size_t nativeSize,
  bool* invalidLogicalPalette = nullptr
) {
  if (invalidLogicalPalette != nullptr) *invalidLogicalPalette = false;
  const size_t pixelCount = static_cast<size_t>(kPayloadWidth) * kPayloadHeight;
  const size_t expectedWireSize = pixelCount / (indexed4 ? 2U : 4U);
  if (wire == nullptr || nativeFrame == nullptr || wireSize != expectedWireSize
      || nativeSize != kPhotoPainterFrameBytes) {
    return false;
  }
  memset(nativeFrame, 0x11, nativeSize);
  for (uint16_t y = 0; y < kPayloadHeight; ++y) {
    for (uint16_t x = 0; x < kPayloadWidth; ++x) {
      const size_t pixel = static_cast<size_t>(y) * kPayloadWidth + x;
      const uint8_t logical = indexed4 ? readPacked4(wire, pixel) : readPacked2(wire, pixel);
      if (indexed4 && !validLogicalIndexed4Color(logical)) {
        if (invalidLogicalPalette != nullptr) *invalidLogicalPalette = true;
        return false;
      }
      const uint8_t nativeColor = indexed4 ? nativeColorFromIndexed4(logical)
                                           : nativeColorFrom2bpp(logical);
      PhysicalCoordinate physical = {0, 0};
      if (!portraitToPhysical(x, y, rotation, physical)
          || !writePacked4(
            nativeFrame, nativeSize, kPhotoPainterWidth, physical.x, physical.y, nativeColor)) {
        return false;
      }
    }
  }
  return true;
}

inline bool isSha256Hex(const char* sha256Hex) {
  if (sha256Hex == nullptr || strlen(sha256Hex) != 64U) return false;
  for (uint8_t index = 0; index < 64; ++index) {
    const char character = sha256Hex[index];
    const bool decimal = character >= '0' && character <= '9';
    const bool lowerHex = character >= 'a' && character <= 'f';
    const bool upperHex = character >= 'A' && character <= 'F';
    if (!decimal && !lowerHex && !upperHex) return false;
  }
  return true;
}

inline uint32_t sourceHash32(const char* sha256Hex) {
  if (!isSha256Hex(sha256Hex)) return 0;
  // Fold the complete SHA-256 string, rather than trusting only its prefix.
  // The full SHA remains authoritative for the network download; this value is
  // a compact cache key whose header is also protected by payload CRC32.
  return crc32(reinterpret_cast<const uint8_t*>(sha256Hex), 64U);
}

inline uint8_t hexNibble(char character) {
  if (character >= '0' && character <= '9') return static_cast<uint8_t>(character - '0');
  if (character >= 'a' && character <= 'f') return static_cast<uint8_t>(character - 'a' + 10);
  if (character >= 'A' && character <= 'F') return static_cast<uint8_t>(character - 'A' + 10);
  return 0xFF;
}

inline bool sha256HexToBytes(const char* sha256Hex, uint8_t* output, size_t outputSize) {
  if (!isSha256Hex(sha256Hex) || output == nullptr || outputSize != kSha256Bytes) return false;
  for (size_t index = 0; index < kSha256Bytes; ++index) {
    const uint8_t high = hexNibble(sha256Hex[index * 2U]);
    const uint8_t low = hexNibble(sha256Hex[index * 2U + 1U]);
    if (high > 0x0F || low > 0x0F) return false;
    output[index] = static_cast<uint8_t>((high << 4U) | low);
  }
  return true;
}

inline CacheHeaderV2 makeCacheHeaderV2(
  const char* sourceSha256,
  DisplayRotation rotation,
  const uint8_t* payload,
  size_t payloadSize
) {
  CacheHeaderV2 header = {};
  header.magic = kCacheMagic;
  header.version = 2;
  header.width = kPhotoPainterWidth;
  header.height = kPhotoPainterHeight;
  header.bitsPerPixel = kNativeBitsPerPixel;
  header.rotation = static_cast<uint8_t>(rotation);
  sha256HexToBytes(sourceSha256, header.sourceSha256, sizeof(header.sourceSha256));
  header.payloadSize = static_cast<uint32_t>(payloadSize);
  header.payloadCrc32 = crc32(payload, payloadSize);
  return header;
}

inline FormalFrameHeader makeFormalFrameHeader(
  const char* sourceSha256,
  DisplayRotation rotation,
  const uint8_t* payload,
  size_t payloadSize
) {
  FormalFrameHeader header = {};
  header.magic = kFormalFrameMagic;
  header.version = kFormalFrameVersion;
  header.width = kPhotoPainterWidth;
  header.height = kPhotoPainterHeight;
  header.bitsPerPixel = kNativeBitsPerPixel;
  header.rotation = static_cast<uint8_t>(rotation);
  header.payloadSize = static_cast<uint32_t>(payloadSize);
  header.payloadCrc32 = crc32(payload, payloadSize);
  sha256HexToBytes(sourceSha256, header.sourceSha256, sizeof(header.sourceSha256));
  return header;
}

inline CacheValidation validateFormalFrameHeader(
  const FormalFrameHeader& header,
  const char* expectedSourceSha256,
  DisplayRotation expectedRotation,
  const uint8_t* payload,
  size_t payloadSize
) {
  uint8_t expected[kSha256Bytes] = {};
  if (!sha256HexToBytes(expectedSourceSha256, expected, sizeof(expected))) {
    return CacheValidation::BadSource;
  }
  if (header.magic != kFormalFrameMagic) return CacheValidation::BadMagic;
  if (header.version != kFormalFrameVersion) return CacheValidation::BadVersion;
  if (header.width != kPhotoPainterWidth || header.height != kPhotoPainterHeight) {
    return CacheValidation::BadDimensions;
  }
  if (header.bitsPerPixel != kNativeBitsPerPixel) return CacheValidation::BadFormat;
  if (header.rotation != static_cast<uint8_t>(expectedRotation)) return CacheValidation::BadRotation;
  if (memcmp(header.sourceSha256, expected, sizeof(expected)) != 0) {
    return CacheValidation::BadSource;
  }
  if (header.payloadSize != kPhotoPainterFrameBytes || payloadSize != header.payloadSize) {
    return CacheValidation::BadLength;
  }
  if (payload == nullptr || crc32(payload, payloadSize) != header.payloadCrc32) {
    return CacheValidation::BadCrc;
  }
  return CacheValidation::Valid;
}

inline CacheValidation validateCacheV2(
  const CacheHeaderV2& header,
  const char* expectedSourceSha256,
  DisplayRotation expectedRotation,
  const uint8_t* payload,
  size_t payloadSize
) {
  uint8_t expected[kSha256Bytes] = {};
  if (!sha256HexToBytes(expectedSourceSha256, expected, sizeof(expected))) return CacheValidation::BadSource;
  if (header.magic != kCacheMagic) return CacheValidation::BadMagic;
  if (header.version != 2) return CacheValidation::BadVersion;
  if (header.width != kPhotoPainterWidth || header.height != kPhotoPainterHeight) {
    return CacheValidation::BadDimensions;
  }
  if (header.bitsPerPixel != kNativeBitsPerPixel) return CacheValidation::BadFormat;
  if (header.rotation != static_cast<uint8_t>(expectedRotation)) return CacheValidation::BadRotation;
  if (memcmp(header.sourceSha256, expected, sizeof(expected)) != 0) return CacheValidation::BadSource;
  if (header.payloadSize != kPhotoPainterFrameBytes || payloadSize != header.payloadSize) {
    return CacheValidation::BadLength;
  }
  if (payload == nullptr || crc32(payload, payloadSize) != header.payloadCrc32) return CacheValidation::BadCrc;
  return CacheValidation::Valid;
}

inline CacheHeader makeCacheHeader(
  uint32_t sourceHash,
  DisplayRotation rotation,
  const uint8_t* payload,
  size_t payloadSize
) {
  CacheHeader header = {
    kCacheMagic,
    kCacheVersion,
    kPhotoPainterWidth,
    kPhotoPainterHeight,
    kNativeBitsPerPixel,
    static_cast<uint8_t>(rotation),
    sourceHash,
    static_cast<uint32_t>(payloadSize),
    crc32(payload, payloadSize),
  };
  return header;
}

inline CacheValidation validateCache(
  const CacheHeader& header,
  uint32_t expectedSourceHash,
  DisplayRotation expectedRotation,
  const uint8_t* payload,
  size_t payloadSize
) {
  if (header.magic != kCacheMagic) return CacheValidation::BadMagic;
  if (header.version != kCacheVersion) return CacheValidation::BadVersion;
  if (header.width != kPhotoPainterWidth || header.height != kPhotoPainterHeight) {
    return CacheValidation::BadDimensions;
  }
  if (header.bitsPerPixel != kNativeBitsPerPixel) return CacheValidation::BadFormat;
  if (header.rotation != static_cast<uint8_t>(expectedRotation)) return CacheValidation::BadRotation;
  if (header.sourceHash != expectedSourceHash) return CacheValidation::BadSource;
  if (header.payloadSize != kPhotoPainterFrameBytes || payloadSize != header.payloadSize) {
    return CacheValidation::BadLength;
  }
  if (payload == nullptr || crc32(payload, payloadSize) != header.payloadCrc32) {
    return CacheValidation::BadCrc;
  }
  return CacheValidation::Valid;
}

}  // namespace inktime
