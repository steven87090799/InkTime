#pragma once
#include <stdint.h>

namespace inktime {
// Reject malformed form values instead of silently converting them to midnight/UTC.
inline bool parsePortalInteger(const char* text, int minimum, int maximum, int& value) {
  if (text == nullptr || *text == '\0') return false;
  bool negative = *text == '-';
  if (negative || *text == '+') ++text;
  if (*text == '\0') return false;
  int parsed = 0;
  for (; *text; ++text) {
    if (*text < '0' || *text > '9' || parsed > 100000) return false;
    parsed = parsed * 10 + (*text - '0');
  }
  if (negative) parsed = -parsed;
  if (parsed < minimum || parsed > maximum) return false;
  value = parsed;
  return true;
}
}  // namespace inktime
