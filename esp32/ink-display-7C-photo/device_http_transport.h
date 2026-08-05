#pragma once

#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFiClient.h>
#include <WiFiClientSecure.h>

namespace inktime {

// Keep the portal field below the practical Preferences/NVS string budget;
// a PEM larger than this is rejected before it can be reported as saved.
constexpr size_t kMaxDeviceCaPemBytes = 3500U;

class DeviceHttpTransport {
 public:
  explicit DeviceHttpTransport(const String &ca_pem) : ca_pem_(ca_pem) {}

  bool begin(
      HTTPClient &http,
      const String &url,
      uint32_t timeout_ms,
      String &error_code,
      String &error_message
  );

  static bool backendUrlAllowed(const String &url, const String &ca_pem, String &error_code);
  static bool trustAnchorValid(const String &ca_pem);

 private:
  String ca_pem_;
  WiFiClient plain_client_;
  WiFiClientSecure secure_client_;
};

}  // namespace inktime
