#include <cassert>
#include <cstring>
#include <vector>

#include "photopainter_core.h"

using namespace inktime;

static uint8_t nativePixel(const std::vector<uint8_t>& frame, uint16_t x, uint16_t y) {
  return readPacked4(frame.data(), static_cast<size_t>(y) * kPhotoPainterWidth + x);
}

int main() {
  static_assert(kPhotoPainterWidth == 800);
  static_assert(kPhotoPainterHeight == 480);
  static_assert(kPhotoPainterFrameBytes == 192000);

#if DEVICE_PROFILE == DEVICE_PROFILE_WAVESHARE_PHOTOPAINTER
  assert(kBoardConfig.profile == DeviceProfile::WavesharePhotoPainter);
  assert(kBoardConfig.display.dc == 8);
  assert(kBoardConfig.sd.cs == 38);
  assert(kBoardConfig.buttons.factoryReset == kNoPin);
  assert(kBoardConfig.buttons.boot == 0);
  assert(kBoardConfig.buttons.user == 4);
  assert(kBoardConfig.requiredFlashBytes == 16U * 1024U * 1024U);
  assert(kBoardConfig.requiredPsramBytes == 8U * 1024U * 1024U);
#else
  assert(kBoardConfig.profile == DeviceProfile::ExistingDefault);
  assert(kBoardConfig.display.dc == 12);
  assert(kBoardConfig.sd.cs == kNoPin);
#endif

  const uint8_t crcSample[] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};
  assert(crc32(crcSample, sizeof(crcSample)) == 0xCBF43926UL);
  const uint8_t shtcSample[] = {0xBE, 0xEF};
  assert(shtc3Crc8(shtcSample, sizeof(shtcSample)) == 0x92);

  assert(nativeColorFromIndexed4(0) == 0);
  assert(nativeColorFromIndexed4(2) == 6);
  assert(nativeColorFromIndexed4(3) == 5);
  assert(nativeColorFromIndexed4(4) == 3);
  assert(nativeColorFromIndexed4(5) == 2);
  assert(nativeColorFromIndexed4(6) == 1);
  assert(nativeColorFrom2bpp(0) == 0);
  assert(nativeColorFrom2bpp(1) == 1);
  assert(nativeColorFrom2bpp(2) == 3);
  assert(nativeColorFrom2bpp(3) == 2);

  std::vector<uint8_t> wire(kPhotoPainterFrameBytes, 0x11);
  assert(writePacked4(wire.data(), wire.size(), kPayloadWidth, 0, 0, 0));
  assert(writePacked4(
    wire.data(), wire.size(), kPayloadWidth, kPayloadWidth - 1,
    kPayloadHeight - 1, 4));
  std::vector<uint8_t> nativeFrame(kPhotoPainterFrameBytes, 0);
  assert(convertWireFrameToNative(
    wire.data(), wire.size(), true, DisplayRotation::Rotate0,
    nativeFrame.data(), nativeFrame.size()));
  assert(nativePixel(nativeFrame, 799, 0) == 0);
  assert(nativePixel(nativeFrame, 0, 479) == 3);

  assert(convertWireFrameToNative(
    wire.data(), wire.size(), true, DisplayRotation::Rotate180,
    nativeFrame.data(), nativeFrame.size()));
  assert(nativePixel(nativeFrame, 0, 479) == 0);
  assert(nativePixel(nativeFrame, 799, 0) == 3);

  const char* sourceSha = "01234567abcdef0123456789abcdef0123456789abcdef0123456789abcdef01";
  const char* changedSha = "01234567abcdef0123456789abcdef0123456789abcdef0123456789abcdef02";
  assert(isSha256Hex(sourceSha));
  assert(!isSha256Hex("01234567"));
  assert(!isSha256Hex("z1234567abcdef0123456789abcdef0123456789abcdef0123456789abcdef01"));
  const uint32_t sourceHash = sourceHash32(sourceSha);
  assert(sourceHash != 0);
  assert(sourceHash != sourceHash32(changedSha));
  CacheHeader header = makeCacheHeader(
    sourceHash, DisplayRotation::Rotate180, nativeFrame.data(), nativeFrame.size());
  assert(validateCache(
    header, sourceHash, DisplayRotation::Rotate180,
    nativeFrame.data(), nativeFrame.size()) == CacheValidation::Valid);
  nativeFrame[100] ^= 0x01;
  assert(validateCache(
    header, sourceHash, DisplayRotation::Rotate180,
    nativeFrame.data(), nativeFrame.size()) == CacheValidation::BadCrc);
  assert(validateCache(
    header, sourceHash, DisplayRotation::Rotate0,
    nativeFrame.data(), nativeFrame.size()) == CacheValidation::BadRotation);

  return 0;
}
