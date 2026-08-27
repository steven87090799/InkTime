#pragma once

#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFiClient.h>
#include <WiFiClientSecure.h>

#include "hardware_profile.h"

// PhotoPainter is designed for direct use with an InkTime host on a trusted
// home LAN.  Other board profiles retain the HTTPS-only default.  Every HTTP
// request still passes the strict RFC1918 literal-IP check in the transport.
#ifndef INKTIME_ALLOW_INSECURE_DEVICE_HTTP
#if DEVICE_PROFILE == DEVICE_PROFILE_WAVESHARE_PHOTOPAINTER
#define INKTIME_ALLOW_INSECURE_DEVICE_HTTP 1
#else
#define INKTIME_ALLOW_INSECURE_DEVICE_HTTP 0
#endif
#endif

namespace inktime {

// Keep the portal field below the practical Preferences/NVS string budget;
// a PEM larger than this is rejected before it can be reported as saved.
constexpr size_t kMaxDeviceCaPemBytes = 3500U;

class DeviceHttpTransport {
 public:
  DeviceHttpTransport() = default;
  explicit DeviceHttpTransport(const String &ca_pem) : ca_pem_(ca_pem) {}

  void configure(const String &ca_pem);

  // A wake owns one transport object.  The underlying client is opened lazily
  // by HTTPClient::begin(), but the trust anchor and origin are fixed for the
  // whole wake and the client is explicitly closed before radio shutdown.
  bool beginSession(
      const String &origin,
      String &error_code,
      String &error_message
  );
  void closeSession();
  bool sessionActive() const { return session_active_; }

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
  String session_origin_;
  bool session_active_ = false;
  WiFiClient plain_client_;
  WiFiClientSecure secure_client_;
};

}  // namespace inktime
