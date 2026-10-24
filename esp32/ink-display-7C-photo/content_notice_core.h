#pragma once

#include "photopainter_core.h"
#include "content_notice_bitmaps.h"

namespace inktime {

enum class ContentNotice : uint8_t {
  None = 0,
  EmptyLibrary = 1,
  AnalysisRequired = 2,
  NoEligiblePhotos = 3,
};

inline ContentNotice parseContentNotice(const char* kind) {
  if (kind == nullptr) return ContentNotice::None;
  if (strcmp(kind, "empty_library") == 0) return ContentNotice::EmptyLibrary;
  if (strcmp(kind, "analysis_required") == 0) return ContentNotice::AnalysisRequired;
  if (strcmp(kind, "no_eligible_photos") == 0) return ContentNotice::NoEligiblePhotos;
  return ContentNotice::None;
}

// Draw into a RAM copy of a verified cached frame. Never overwrite its stored
// bytes/hash or turn a diagnostic overlay into a photo-display acknowledgement.
inline bool overlayContentNotice(
    uint8_t* frame, size_t length, ContentNotice kind, DisplayRotation rotation) {
  const uint8_t* bitmap = nullptr;
  switch (kind) {
    case ContentNotice::EmptyLibrary: bitmap = kContentNoticeEmptyLibrary; break;
    case ContentNotice::AnalysisRequired: bitmap = kContentNoticeAnalysisRequired; break;
    case ContentNotice::NoEligiblePhotos: bitmap = kContentNoticeNoEligiblePhotos; break;
    default: return false;
  }
  constexpr uint16_t height = 44;
  if (frame == nullptr || length != kPhotoPainterFrameBytes
      || kPhotoPainterWidth < kContentNoticeTextWidth || kPhotoPainterHeight < height
      || (rotation != DisplayRotation::Rotate0 && rotation != DisplayRotation::Rotate180)) return false;
  const uint16_t left = (kPhotoPainterWidth - kContentNoticeTextWidth) / 2U;
  const uint16_t top = (height - kContentNoticeTextHeight) / 2U;
  for (uint16_t row = 0; row < height; ++row) {
    for (uint16_t x = 0; x < kPhotoPainterWidth; ++x) {
      uint8_t color = row < 2U ? 0U : 1U;  // black separator, white background
      if (row >= top && row < top + kContentNoticeTextHeight
          && x >= left && x < left + kContentNoticeTextWidth) {
        const size_t bit = (row - top) * kContentNoticeTextWidth + x - left;
        if (bitmap[bit / 8U] & (0x80U >> (bit % 8U))) color = 0U;
      }
      uint16_t px = x;
      uint16_t py = kPhotoPainterHeight - height + row;
      if (rotation == DisplayRotation::Rotate180) {
        px = kPhotoPainterWidth - 1U - px;
        py = kPhotoPainterHeight - 1U - py;
      }
      if (!writePacked4(frame, length, kPhotoPainterWidth, px, py, color)) return false;
    }
  }
  return true;
}

}  // namespace inktime
