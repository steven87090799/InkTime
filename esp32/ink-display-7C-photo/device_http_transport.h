#pragma once

#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFiClient.h>
#include <WiFiClientSecure.h>

namespace inktime {

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
