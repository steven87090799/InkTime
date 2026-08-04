#include "device_http_transport.h"

#include <ctype.h>

#ifndef INKTIME_ALLOW_INSECURE_DEVICE_HTTP
#define INKTIME_ALLOW_INSECURE_DEVICE_HTTP 0
#endif

#ifndef INKTIME_DEVICE_ROOT_CA
#define INKTIME_DEVICE_ROOT_CA ""
#endif

namespace {

String hostFromUrl(const String &url) {
  const int scheme = url.indexOf("://");
  if (scheme < 0) return String("");
  int start = scheme + 3;
  int end = url.indexOf('/', start);
  if (end < 0) end = url.length();
  int query = url.indexOf('?', start);
  if (query >= 0 && query < end) end = query;
  int fragment = url.indexOf('#', start);
  if (fragment >= 0 && fragment < end) end = fragment;
  String authority = url.substring(start, end);
  const int at = authority.lastIndexOf('@');
  if (at >= 0) return String("");
  const int colon = authority.lastIndexOf(':');
  if (colon >= 0 && authority.indexOf(']') < colon) authority = authority.substring(0, colon);
  authority.toLowerCase();
  return authority;
}

bool isPrivateHost(const String &host) {
  if (host == "localhost" || host.endsWith(".local") || host.endsWith(".lan") || host.endsWith(".internal")) {
    return true;
  }
  if (host == "127.0.0.1" || host == "::1") return true;
  if (host.startsWith("10.") || host.startsWith("192.168.")) return true;
  if (host.startsWith("172.")) {
    int dot = host.indexOf('.', 4);
    int second = host.substring(4, dot < 0 ? host.length() : dot).toInt();
    return second >= 16 && second <= 31;
  }
  return false;
}

bool validCa(const String &ca) {
  return ca.length() >= 64U && ca.length() <= inktime::kMaxDeviceCaPemBytes
      && ca.indexOf("-----BEGIN CERTIFICATE-----") >= 0
      && ca.indexOf("-----END CERTIFICATE-----") >= 0;
}

String effectiveCa(const String &configured) {
  return configured.length() > 0 ? configured : String(INKTIME_DEVICE_ROOT_CA);
}

}  // namespace

namespace inktime {

bool DeviceHttpTransport::backendUrlAllowed(const String &url, const String &ca_pem, String &error_code) {
  error_code = "";
  const bool https = url.startsWith("https://");
  const bool http = url.startsWith("http://");
  const String host = hostFromUrl(url);
  if ((!https && !http) || host.length() == 0 || url.indexOf('@') >= 0
      || url.indexOf('#') >= 0) {
    error_code = "DEVICE-URL-INVALID";
    return false;
  }
  if (https && !validCa(effectiveCa(ca_pem))) {
    error_code = "DEVICE-TLS-NO-TRUST";
    return false;
  }
#if INKTIME_ALLOW_INSECURE_DEVICE_HTTP
  if (http && !isPrivateHost(host)) {
    error_code = "DEVICE-HTTP-PUBLIC-DISALLOWED";
    return false;
  }
  return true;
#else
  if (http) {
    error_code = "DEVICE-HTTP-DISALLOWED";
    return false;
  }
  return true;
#endif
}

bool DeviceHttpTransport::trustAnchorValid(const String &ca_pem) {
  return validCa(ca_pem);
}

bool DeviceHttpTransport::begin(
    HTTPClient &http,
    const String &url,
    uint32_t timeout_ms,
    String &error_code,
    String &error_message
) {
  if (!backendUrlAllowed(url, ca_pem_, error_code)) {
    error_message = "Backend URL 或 TLS trust anchor 不符合裝置安全政策";
    return false;
  }
  http.setConnectTimeout(10000);
  http.setTimeout(timeout_ms);
  http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
  if (url.startsWith("https://")) {
    const String ca = effectiveCa(ca_pem_);
    secure_client_.setCACert(ca.c_str());
    if (!http.begin(secure_client_, url)) {
      error_code = "DEVICE-TLS-BEGIN";
      error_message = "HTTPS client 初始化失敗";
      return false;
    }
    return true;
  }
#if INKTIME_ALLOW_INSECURE_DEVICE_HTTP
  if (!http.begin(plain_client_, url)) {
    error_code = "DEVICE-HTTP-BEGIN";
    error_message = "HTTP client 初始化失敗";
    return false;
  }
  return true;
#else
  error_code = "DEVICE-HTTP-DISALLOWED";
  error_message = "正式韌體禁止 HTTP";
  return false;
#endif
}

}  // namespace inktime
