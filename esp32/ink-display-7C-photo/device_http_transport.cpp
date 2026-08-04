#include "device_http_transport.h"

#include <mbedtls/x509_crt.h>
#include <ctype.h>

#include <lwip/inet.h>
#include <lwip/ip4_addr.h>
#include <lwip/ip6_addr.h>

#ifndef INKTIME_ALLOW_INSECURE_DEVICE_HTTP
#define INKTIME_ALLOW_INSECURE_DEVICE_HTTP 0
#endif

#ifndef INKTIME_ALLOW_INSECURE_DEVICE_HTTP_HOSTNAMES
#define INKTIME_ALLOW_INSECURE_DEVICE_HTTP_HOSTNAMES 0
#endif

#ifndef INKTIME_DEVICE_ROOT_CA
#define INKTIME_DEVICE_ROOT_CA ""
#endif

namespace {

String hostFromUrl(const String &url) {
  const int scheme = url.indexOf("://");
  if (scheme < 0) return String("");
  int start = scheme + 3;
  String authority = url.substring(start);
  const int at = authority.indexOf('@');
  if (at >= 0) return String("");
  if (authority.startsWith("[")) {
    const int close = authority.indexOf(']');
    if (close <= 1) return String("");
    if (close + 1 < authority.length() && authority[close + 1] != ':') return String("");
    authority = authority.substring(1, close);
  } else {
    const int colon = authority.lastIndexOf(':');
    if (colon >= 0) {
      if (colon == 0 || authority.indexOf(':') != colon) return String("");
      authority = authority.substring(0, colon);
    }
  }
  authority.toLowerCase();
  return authority;
}

bool validAuthorityPort(const String &authority) {
  String port;
  if (authority.startsWith("[")) {
    const int close = authority.indexOf(']');
    if (close <= 1) return false;
    if (close + 1 == authority.length()) return true;
    if (authority[close + 1] != ':') return false;
    port = authority.substring(close + 2);
  } else {
    const int colon = authority.lastIndexOf(':');
    if (colon < 0) return authority.length() > 0U;
    if (colon == 0 || authority.indexOf(':') != colon) return false;
    port = authority.substring(colon + 1);
  }
  if (port.length() == 0U || port.length() > 5U) return false;
  for (size_t index = 0U; index < port.length(); ++index) {
    if (port[index] < '0' || port[index] > '9') return false;
  }
  const long number = port.toInt();
  return number >= 1L && number <= 65535L;
}

bool isPrivateLiteralHost(const String &host) {
  ip4_addr_t ipv4;
  if (ip4addr_aton(host.c_str(), &ipv4)) {
    const uint32_t value = lwip_ntohl(ip4_addr_get_u32(&ipv4));
    const uint8_t first = static_cast<uint8_t>(value >> 24U);
    const uint8_t second = static_cast<uint8_t>((value >> 16U) & 0xffU);
    return first == 10U || first == 127U || (first == 169U && second == 254U)
      || (first == 172U && second >= 16U && second <= 31U)
      || (first == 192U && second == 168U);
  }
  ip6_addr_t ipv6;
  if (ip6addr_aton(host.c_str(), &ipv6)) {
    const uint32_t first_block = lwip_ntohl(ipv6.addr[0]);
    const uint8_t first = static_cast<uint8_t>(first_block >> 24U);
    const uint8_t second = static_cast<uint8_t>((first_block >> 16U) & 0xffU);
    return ip6_addr_isloopback(&ipv6)
      || (first & 0xfeU) == 0xfcU
      || (first == 0xfeU && (second & 0xc0U) == 0x80U);
  }
  return false;
}

bool isLiteralIpHost(const String &host) {
  ip4_addr_t ipv4;
  if (ip4addr_aton(host.c_str(), &ipv4)) return true;
  ip6_addr_t ipv6;
  return ip6addr_aton(host.c_str(), &ipv6);
}

bool validCa(const String &ca) {
  if (ca.length() < 64U || ca.length() > inktime::kMaxDeviceCaPemBytes
      || ca.indexOf("-----BEGIN CERTIFICATE-----") < 0
      || ca.indexOf("-----END CERTIFICATE-----") < 0
      || ca.indexOf("-----BEGIN PRIVATE KEY-----") >= 0
      || ca.indexOf("-----BEGIN RSA PRIVATE KEY-----") >= 0) {
    return false;
  }
  mbedtls_x509_crt certificate;
  mbedtls_x509_crt_init(&certificate);
  const int result = mbedtls_x509_crt_parse(
      &certificate,
      reinterpret_cast<const unsigned char*>(ca.c_str()),
      ca.length() + 1U);
  const bool valid = result == 0 && certificate.raw.p != nullptr && certificate.raw.len > 0U;
  mbedtls_x509_crt_free(&certificate);
  return valid;
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
  const int authority_start = url.indexOf("://") + 3;
  const String authority = authority_start >= 3 ? url.substring(authority_start) : String("");
  if ((!https && !http) || host.length() == 0 || authority.indexOf('/') >= 0
      || authority.indexOf('?') >= 0 || authority.indexOf('#') >= 0
      || authority.indexOf('\\') >= 0 || !validAuthorityPort(authority)) {
    error_code = "DEVICE-URL-INVALID";
    return false;
  }
  for (size_t index = 0U; index < url.length(); ++index) {
    if (url[index] < 0x20 || url[index] == 0x7f) {
      error_code = "DEVICE-URL-INVALID";
      return false;
    }
  }
  if (https && !validCa(effectiveCa(ca_pem))) {
    error_code = "DEVICE-TLS-CA-INVALID";
    return false;
  }
#if INKTIME_ALLOW_INSECURE_DEVICE_HTTP
  if (http && !isPrivateLiteralHost(host)
      && (isLiteralIpHost(host) || !INKTIME_ALLOW_INSECURE_DEVICE_HTTP_HOSTNAMES)) {
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
