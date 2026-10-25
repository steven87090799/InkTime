#pragma once
#include <stdint.h>
#include <cctype>
#include <string>

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

// The portal validates origins before comparing them. Match the legacy
// HTTPS default and avoid replacing credentials for cosmetic URL edits.
inline std::string portalServerOrigin(const char* text) {
  std::string origin = text == nullptr ? "" : text;
  while (!origin.empty() && std::isspace(static_cast<unsigned char>(origin.front()))) origin.erase(0, 1);
  while (!origin.empty() && std::isspace(static_cast<unsigned char>(origin.back()))) origin.pop_back();
  for (char& ch : origin) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
  if (!origin.empty() && origin.find("://") == std::string::npos) origin = "https://" + origin;
  while (!origin.empty() && origin.back() == '/') origin.pop_back();
  const std::string default_port = origin.find("https://") == 0U ? ":443" : ":80";
  if (origin.size() > default_port.size()
      && origin.compare(origin.size() - default_port.size(), default_port.size(), default_port) == 0) {
    origin.erase(origin.size() - default_port.size());
  }
  return origin;
}

// Only an authenticated, physically entered portal may request this reset.
// Never invoke it for an ordinary authentication failure or network wake.
template <typename Config>
inline bool preparePortalPairing(
    Config& candidate, const char* previous_origin, const char* next_origin,
    bool explicitly_requested) {
  if (!explicitly_requested && portalServerOrigin(previous_origin) == portalServerOrigin(next_origin)) return false;
  candidate.device_token = "";
  candidate.device_secret = "";
  candidate.auth_state = "unpaired";
  candidate.credential_version = 0U;
  candidate.pairing_id = "";
  candidate.pairing_nonce = "";
  candidate.pairing_expires_at_epoch = 0U;
  candidate.pairing_retry_at_epoch = 0U;
  candidate.pairing_retry_attempt = 0U;
  candidate.config_version = 0U;
  return true;
}
}  // namespace inktime
