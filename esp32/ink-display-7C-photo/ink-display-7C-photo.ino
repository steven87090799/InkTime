#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <SPI.h>
#include <time.h>
#include <sys/time.h>
#include "esp_heap_caps.h"
#include "esp_system.h"

#include "hardware_profile.h"
#include "photopainter_core.h"
#if INKTIME_PHOTOPAINTER_ENABLED
#include "photopainter_support.h"
#include "power_manager.h"
#else
#include <GxEPD2_7C.h>
#endif
#include <HardwareSerial.h>
#include "esp_wifi.h"
#include "esp_bt.h"
#include "mbedtls/sha256.h"
#include "mbedtls/version.h"

#include "driver/gpio.h"
#include "driver/rtc_io.h"
#include "soc/soc_caps.h"

// =======================
//  正式版預設不輸出逐步序列 Log；需要除錯時以 -DINKTIME_DEBUG_LOG=1 編譯。
// =======================
#ifndef INKTIME_DEBUG_LOG
#define INKTIME_DEBUG_LOG 0
#endif
#define DEBUG_LOG INKTIME_DEBUG_LOG

HardwareSerial DebugSerial(0);

using inktime::kBoardConfig;

#if INKTIME_PHOTOPAINTER_ENABLED
inktime::PhotoPainterSupport photoPainter(kBoardConfig);
#endif

#if DEBUG_LOG
  #define DBG_BEGIN()    DebugSerial.begin(115200)
  #define DBG_PRINT(x)   DebugSerial.print(x)
  #define DBG_PRINTLN(x) DebugSerial.println(x)
#else
  #define DBG_BEGIN()
  #define DBG_PRINT(x)
  #define DBG_PRINTLN(x)
#endif

static const uint32_t FACTORY_RESET_SAMPLE_DELAY_MS = 5;

// =======================
//  AP 配置页保底：进入 AP 后 5 分钟没保存配置 -> 睡到“下一个刷新点”
// =======================
static const uint32_t AP_TIMEOUT_MS = 5UL * 60UL * 1000UL; // 5 分钟

// 實體面板固定 800x480；既有伺服器 payload 契約維持直向 480x800。
static constexpr int EPD_WIDTH  = kBoardConfig.display.width;
static constexpr int EPD_HEIGHT = kBoardConfig.display.height;
static constexpr int FB_WIDTH   = kBoardConfig.payloadWidth;
static constexpr int FB_HEIGHT  = kBoardConfig.payloadHeight;

#if !INKTIME_PHOTOPAINTER_ENABLED
GxEPD2_7C<
  INKTIME_PANEL_CLASS,
  INKTIME_PANEL_CLASS::HEIGHT / 4
> display(
  INKTIME_PANEL_CLASS(
    kBoardConfig.display.spi.cs,
    kBoardConfig.display.dc,
    kBoardConfig.display.reset,
    kBoardConfig.display.busy
  )
);
#endif

// =======================
//  舊版 URL 金鑰 API 已停用；新版透過每台裝置獨立 Bearer Token 取得 Manifest。
// =======================
#define DEVICE_MANIFEST_PATH "/api/device/v1/releases/latest"
#define DEVICE_STATUS_PATH   "/api/device/v1/status"
#define INKTIME_FIRMWARE_VERSION "2.3.0"

// No trusted CA provisioning exists yet. HTTPS is rejected by default instead
// of silently downgrading certificate verification. Isolated LAN HTTP remains
// supported; an explicit development override prints a warning.
#ifndef INKTIME_ALLOW_UNVERIFIED_HTTPS
#define INKTIME_ALLOW_UNVERIFIED_HTTPS 0
#endif

// =======================
//  配置存储 / WiFi / WebServer
// =======================
Preferences prefs;
WebServer  server(80);

struct Config {
  String  wifi_ssid;
  String  wifi_pass;
  String  backend_hostport;
  String  device_token;
  int32_t tz_offset_minutes;
  uint8_t refresh_hour;
  uint8_t refresh_minute;
  bool    rotate180;
  uint32_t config_version;
  bool    valid;
};

const char*  DEFAULT_HOSTPORT = "";
const int32_t DEFAULT_TZ_MINUTES = 8 * 60;
const uint8_t DEFAULT_HOUR    = 8;
const uint8_t DEFAULT_MINUTE  = 0;

Config g_cfg;
uint8_t* frameData = nullptr;
size_t frameDataSize = 0;
bool frameIndexed4 = false;
bool frameNativePalette = false;
bool serverConfigChanged = false;
String currentReleaseId;
String currentRenderProfile;
String lastDeviceErrorCode;
String lastDeviceErrorMessage;

static int calculateSha256(const unsigned char* input, size_t length, unsigned char output[32]) {
#if MBEDTLS_VERSION_MAJOR >= 3
  return mbedtls_sha256(input, length, output, 0);
#else
  return mbedtls_sha256_ret(input, length, output, 0);
#endif
}

static bool backendTransportAllowed(const String &base) {
  if (!base.startsWith("https://")) return true;
#if INKTIME_ALLOW_UNVERIFIED_HTTPS
#if DEBUG_LOG
  DBG_PRINTLN("[TLS] WARNING: unverified HTTPS override is enabled");
#endif
  return true;
#else
  lastDeviceErrorCode = "DEVICE-TLS-UNCONFIGURED";
  lastDeviceErrorMessage = "HTTPS 尚未配置可信 CA，已拒絕未驗證連線";
  return false;
#endif
}

static void releaseAllGpioHoldsAtBoot() {
  gpio_deep_sleep_hold_dis();
  for (int gpio = 0; gpio <= 48; ++gpio) {
    gpio_num_t gn = (gpio_num_t)gpio;
    if (!GPIO_IS_VALID_GPIO(gn)) continue;
    gpio_hold_dis(gn);
    if (rtc_gpio_is_valid_gpio(gn)) rtc_gpio_hold_dis(gn);
  }
}

static void clearConfigNVS() {
#if DEBUG_LOG
  DBG_PRINTLN("[NVS] clearConfigNVS()");
#endif
  prefs.begin("dashcfg", false);
  prefs.clear();
  prefs.end();
}

static bool isFactoryResetRequestedAtBoot() {
  if (kBoardConfig.buttons.factoryReset == inktime::kNoPin) return false;
  pinMode(
    kBoardConfig.buttons.factoryReset,
    kBoardConfig.buttons.factoryResetActiveLow ? INPUT_PULLUP : INPUT_PULLDOWN
  );
  delay(FACTORY_RESET_SAMPLE_DELAY_MS);
  const int activeLevel = kBoardConfig.buttons.factoryResetActiveLow ? LOW : HIGH;
  return digitalRead(kBoardConfig.buttons.factoryReset) == activeLevel;
}

static void saveLastTimeEpoch(time_t epoch) {
  prefs.begin("dashcfg", false);
  prefs.putULong("last_epoch", (uint32_t)epoch);
  prefs.end();
#if DEBUG_LOG
  DBG_PRINT("[TIME] save last_epoch="); DBG_PRINTLN((uint32_t)epoch);
#endif
}

static bool loadLastTimeEpoch(time_t &epochOut) {
  prefs.begin("dashcfg", true);
  uint32_t v = prefs.getULong("last_epoch", 0);
  prefs.end();
  if (v == 0) return false;
  epochOut = (time_t)v;
  return true;
}

#if INKTIME_PHOTOPAINTER_ENABLED
static bool loadLastPhotoIndex(size_t fileCount, size_t &indexOut) {
  if (fileCount == 0) return false;
  prefs.begin("dashcfg", true);
  const uint32_t value = prefs.getULong("photo_idx", UINT32_MAX);
  prefs.end();
  if (value == UINT32_MAX || value >= fileCount) return false;
  indexOut = static_cast<size_t>(value);
  return true;
}

static void saveLastPhotoIndex(size_t index) {
  prefs.begin("dashcfg", false);
  prefs.putULong("photo_idx", static_cast<uint32_t>(index));
  prefs.end();
}
#endif

static uint32_t minutesToNextRefreshFromLastEpoch(const Config &cfg) {
  time_t lastEpoch;
  if (!loadLastTimeEpoch(lastEpoch)) {
    return 1440;
  }

  struct tm t;
  localtime_r(&lastEpoch, &t);

  int curMinOfDay = t.tm_hour * 60 + t.tm_min;
  int targetMin   = (int)cfg.refresh_hour * 60 + (int)cfg.refresh_minute;
  int deltaMin;

  if (curMinOfDay < targetMin) deltaMin = targetMin - curMinOfDay;
  else                         deltaMin = 24 * 60 - (curMinOfDay - targetMin);

  if (deltaMin < 1) deltaMin = 24 * 60;
  if (deltaMin > 1440) deltaMin = 1440;
  return (uint32_t)deltaMin;
}

// =======================
//  配置读写
// =======================
void loadConfig(Config &cfg) {
  prefs.begin("dashcfg", true); // read-only
  cfg.wifi_ssid        = prefs.getString("ssid", "");
  cfg.wifi_pass        = prefs.getString("pass", "");
  cfg.backend_hostport = prefs.getString("hostport", DEFAULT_HOSTPORT);
  cfg.device_token     = prefs.getString("devtoken", "");
  cfg.tz_offset_minutes = prefs.getInt("tzmin", prefs.getInt("tz", 8) * 60);
  cfg.refresh_hour     = (uint8_t)prefs.getUChar("hour", DEFAULT_HOUR);
  cfg.refresh_minute   = (uint8_t)prefs.getUChar("minute", DEFAULT_MINUTE);
  cfg.rotate180        = prefs.getBool("rot180", false);
  cfg.config_version   = prefs.getULong("cfgver", 0);
  prefs.end();

  cfg.valid = (cfg.wifi_ssid.length() > 0);

#if DEBUG_LOG
  DBG_PRINTLN("---- loadConfig ----");
  DBG_PRINT("[CFG] ssid="); DBG_PRINTLN(cfg.wifi_ssid);
  DBG_PRINT("[CFG] hostport="); DBG_PRINTLN(cfg.backend_hostport);
  DBG_PRINT("[CFG] tz_offset_minutes="); DBG_PRINTLN(cfg.tz_offset_minutes);
  DBG_PRINT("[CFG] refresh_hour="); DBG_PRINTLN((int)cfg.refresh_hour);
  DBG_PRINT("[CFG] refresh_minute="); DBG_PRINTLN((int)cfg.refresh_minute);
  DBG_PRINT("[CFG] rotate180="); DBG_PRINTLN(cfg.rotate180 ? "true" : "false");
  DBG_PRINT("[CFG] valid="); DBG_PRINTLN(cfg.valid ? "true" : "false");
#endif
}

void saveConfig(const Config &cfg) {
  prefs.begin("dashcfg", false);
  prefs.putString("ssid", cfg.wifi_ssid);
  prefs.putString("pass", cfg.wifi_pass);
  prefs.putString("hostport", cfg.backend_hostport);
  prefs.putString("devtoken", cfg.device_token);
  prefs.putInt("tzmin", cfg.tz_offset_minutes);
  prefs.putUChar("hour", cfg.refresh_hour);
  prefs.putUChar("minute", cfg.refresh_minute);
  prefs.putBool("rot180", cfg.rotate180);
  prefs.putULong("cfgver", cfg.config_version);
  prefs.end();

#if DEBUG_LOG
  DBG_PRINTLN("[CFG] saved");
#endif
}

// =======================
//  HTML 工具
// =======================
String htmlEscape(const String &s) {
  String out;
  out.reserve(s.length());
  for (size_t i = 0; i < s.length(); ++i) {
    char c = s[i];
    if      (c == '&')  out += F("&amp;");
    else if (c == '<')  out += F("&lt;");
    else if (c == '>')  out += F("&gt;");
    else if (c == '"')  out += F("&quot;");
    else                out += c;
  }
  return out;
}

static void wifiHardResetForPortal() {
#if DEBUG_LOG
  DBG_PRINTLN("[WIFI] wifiHardResetForPortal()");
#endif
  WiFi.scanDelete();
  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);
  delay(200);

  WiFi.mode(WIFI_AP_STA);

  WiFi.setSleep(false);
  esp_wifi_set_ps(WIFI_PS_NONE);

  WiFi.scanDelete();
  delay(50);
}

String buildConfigPage() {
  WiFi.scanDelete();
  delay(30);

  int n = WiFi.scanNetworks(/*async=*/false, /*hidden=*/true);

#if DEBUG_LOG
  DBG_PRINT("[CFG] scanNetworks n="); DBG_PRINTLN(n);
#endif

  String curSsid = g_cfg.wifi_ssid;
  String host    = htmlEscape(g_cfg.backend_hostport);
  int32_t tz     = g_cfg.tz_offset_minutes / 60;
  if (tz < -12 || tz > 14) tz = DEFAULT_TZ_MINUTES / 60;
  uint8_t hour   = g_cfg.refresh_hour;
  if (hour > 23) hour = DEFAULT_HOUR;
  uint8_t minute = g_cfg.refresh_minute;
  if (minute > 59) minute = DEFAULT_MINUTE;
  bool rot180    = g_cfg.rotate180;

  String html;
  html.reserve(4096);

  html += F("<!DOCTYPE html><html><head><meta charset='utf-8'>");
  html += F("<meta name='viewport' content='width=device-width,initial-scale=1'>");
  html += F("<title>InkTime 設定</title></head><body>");
  html += F("<h2>InkTime 首次配對</h2>");
  html += F("<form method='POST' action='/save'>");

  html += F("WiFi SSID:<br>");
  html += F("<select id='ssid_select' style='width: 288px;' onchange=\"document.getElementById('ssid_input').value=this.value;\">");
  html += F("<option value=''>（手動輸入或選擇）</option>");
  if (n > 0) {
    for (int i = 0; i < n; ++i) {
      String s = WiFi.SSID(i);
      if (s.length() == 0) continue;
      String esc = htmlEscape(s);
      html += F("<option value='");
      html += esc;
      html += F("'");
      if (s == curSsid) html += F(" selected");
      html += F(">");
      html += esc;
      html += F("</option>");
    }
  }
  html += F("</select><br>");
  html += F("<input id='ssid_input' name='ssid' style='width: 280px;' value='");
  html += htmlEscape(curSsid);
  html += F("'><br><br>");

  html += F("密碼:<br><input name='pass' type='password' style='width: 280px;'><br><br>");

  html += F("InkTime 伺服器 (http://host:port):<br><input name='hostport' size='40' value='");
  html += host;
  html += F("'><br><br>");

  html += F("裝置 Token（留空會保留現有 Token）：<br><input name='device_token' type='password' size='48' autocomplete='off'><br>");
  html += F("<small>請從 InkTime 裝置管理頁配對；Token 不會顯示在網址或序列埠。</small><br><br>");

  html += F("備援刷新時間（連上伺服器後改由 Web 設定）：<br><select name='hour'>");
  for (int h = 0; h < 24; ++h) {
    html += "<option value='";
    html += String(h);
    html += "'";
    if (h == hour) html += " selected";
    html += ">";
    html += String(h);
    html += F(" 時</option>");
  }
  html += F("</select><select name='minute'>");
  for (int m = 0; m < 60; m += 5) {
    html += "<option value='";
    html += String(m);
    html += "'";
    if (m == minute) html += " selected";
    html += ">";
    if (m < 10) html += "0";
    html += String(m);
    html += F(" 分</option>");
  }
  html += F("</select><br><br>");

  html += F("備援 UTC 時區偏移:<br><select name='tz'>");
  for (int t = -12; t <= 14; ++t) {
    html += "<option value='";
    html += String(t);
    html += "'";
    if (t == tz) html += " selected";
    html += ">";
    if (t >= 0) html += "+";
    html += String(t);
    html += F("</option>");
  }
  html += F("</select><br><br>");

  html += F("<label><input type='checkbox' name='rot180' value='1'");
  if (rot180) html += F(" checked");
  html += F("> 畫面旋轉 180°</label><br><br>");

  if (n <= 0) {
    html += F("<p style='color:#c00'>未掃描到 Wi-Fi，可直接在上方輸入框手動填寫 SSID。</p>");
  }

  html += F("<input type='submit' value='儲存並重新啟動'>");
  html += F("</form></body></html>");

  return html;
}

// =======================
//  WebServer 处理
// =======================
void handleRoot() {
#if DEBUG_LOG
  DBG_PRINTLN("[HTTP] GET /");
#endif
  server.send(200, "text/html; charset=utf-8", buildConfigPage());
}

void handleSave() {
#if DEBUG_LOG
  DBG_PRINTLN("[HTTP] POST /save");
#endif
  String ssid     = server.arg("ssid");
  String pass     = server.arg("pass");
  String host     = server.arg("hostport");
  String deviceToken = server.arg("device_token");
  String hourStr  = server.arg("hour");
  String minuteStr = server.arg("minute");
  String tzStr    = server.arg("tz");
  bool rot180Req  = (server.arg("rot180") == "1");

  ssid.trim();
  host.trim();
  deviceToken.trim();

  Config newCfg = g_cfg;

  if (ssid.length() > 0) newCfg.wifi_ssid = ssid;
  if (pass.length() > 0) newCfg.wifi_pass = pass;

  newCfg.backend_hostport = host;
  if (deviceToken.length() > 0) newCfg.device_token = deviceToken;

  int32_t tz = tzStr.toInt();
  if (tz < -12) tz = -12;
  if (tz > 14)  tz = 14;
  newCfg.tz_offset_minutes = tz * 60;

  int hour = hourStr.toInt();
  if (hour < 0)  hour = 0;
  if (hour > 23) hour = 23;
  newCfg.refresh_hour = (uint8_t)hour;
  int minute = minuteStr.toInt();
  if (minute < 0) minute = 0;
  if (minute > 59) minute = 59;
  newCfg.refresh_minute = (uint8_t)minute;

  newCfg.rotate180 = rot180Req;
  newCfg.valid     = (newCfg.wifi_ssid.length() > 0);

  saveConfig(newCfg);

  server.send(
    200,
    "text/html; charset=utf-8",
    F("<html><body><h3>儲存成功，裝置即將重新啟動...</h3></body></html>")
  );

  delay(800);
  ESP.restart();
}

// =======================
//  Deep Sleep 前
// =======================
void prepareDeepSleepDomains() {
#if defined(SOC_PM_SUPPORT_RTC_PERIPH_PD) && SOC_PM_SUPPORT_RTC_PERIPH_PD
#if INKTIME_PHOTOPAINTER_ENABLED
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH,    ESP_PD_OPTION_AUTO);
#else
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH,    ESP_PD_OPTION_OFF);
#endif
#endif
#if defined(SOC_PM_SUPPORT_RTC_SLOW_MEM_PD) && SOC_PM_SUPPORT_RTC_SLOW_MEM_PD
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_SLOW_MEM,  ESP_PD_OPTION_OFF);
#endif
#if defined(SOC_PM_SUPPORT_RTC_FAST_MEM_PD) && SOC_PM_SUPPORT_RTC_FAST_MEM_PD
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_FAST_MEM,  ESP_PD_OPTION_OFF);
#endif
}

// =======================
//  关闭墨水屏相关引脚，提升续航表现
// =======================
static void powerDownEPD() {
  const int epdPins[] = {
    kBoardConfig.display.busy,
    kBoardConfig.display.reset,
    kBoardConfig.display.dc,
    kBoardConfig.display.spi.cs,
    kBoardConfig.display.spi.sck,
    kBoardConfig.display.spi.mosi,
  };
  for (size_t i = 0; i < sizeof(epdPins)/sizeof(epdPins[0]); ++i) {
    int p = epdPins[i];
    if (p == inktime::kNoPin) continue;
    pinMode(p, INPUT);
    pinMode(p, INPUT_PULLDOWN);
  }
}

static void deepSleepHoldOnlyEpdPins() {
  const int epdPins[] = {
    kBoardConfig.display.busy,
    kBoardConfig.display.reset,
    kBoardConfig.display.dc,
    kBoardConfig.display.spi.cs,
    kBoardConfig.display.spi.sck,
    kBoardConfig.display.spi.mosi,
  };
  for (size_t i = 0; i < sizeof(epdPins)/sizeof(epdPins[0]); ++i) {
    if (epdPins[i] == inktime::kNoPin) continue;
    gpio_num_t gn = (gpio_num_t)epdPins[i];
    if (!GPIO_IS_VALID_GPIO(gn)) continue;

    gpio_set_direction(gn, GPIO_MODE_INPUT);
    gpio_pulldown_en(gn);
    gpio_pullup_dis(gn);
    gpio_hold_en(gn);

    if (rtc_gpio_is_valid_gpio(gn)) rtc_gpio_isolate(gn);
  }
  gpio_deep_sleep_hold_en();
}

// =======================
//  Deep Sleep
// =======================
void goDeepSleepMinutes(uint32_t minutes) {
  if (minutes < 1)    minutes = 1;
  if (minutes > 1440) minutes = 1440;

#if DEBUG_LOG
  DBG_PRINT("[SLEEP] minutes="); DBG_PRINTLN((int)minutes);
#endif

  uint64_t us = (uint64_t)minutes * 60ULL * 1000000ULL;

#if INKTIME_PHOTOPAINTER_ENABLED
  photoPainter.prepareForDeepSleep();
  photoPainter.enableWakeSources();
#endif

  if (frameData) {
    heap_caps_free(frameData);
    frameData = nullptr;
    frameDataSize = 0;
  }

  powerDownEPD();

  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);
  esp_wifi_stop();

#if defined(CONFIG_BT_ENABLED)
  esp_bt_controller_disable();
#endif

  deepSleepHoldOnlyEpdPins();

  prepareDeepSleepDomains();
  esp_sleep_enable_timer_wakeup(us);

#if DEBUG_LOG
  DBG_PRINTLN("[SLEEP] go deep sleep");
#endif
  esp_deep_sleep_start();
}

// =======================
//  启动 AP 配置模式
// =======================
void startConfigPortal() {
#if DEBUG_LOG
  DBG_PRINTLN("[CFG] enter startConfigPortal()");
#endif

  wifiHardResetForPortal();

  String chipHex = String((uint32_t)ESP.getEfuseMac(), HEX);
  chipHex.toUpperCase();
  while (chipHex.length() < 8) chipHex = "0" + chipHex;
  String shortId = chipHex.substring(chipHex.length() - 6);
  String apSsid = "InkTime-" + shortId;
  String apPassword = "InkTime" + shortId;

  bool apOk = WiFi.softAP(apSsid.c_str(), apPassword.c_str());
  (void)apOk;

#if DEBUG_LOG
  DBG_PRINT("[CFG] softAP result = "); DBG_PRINTLN(apOk ? "OK" : "FAIL");
  DBG_PRINT("[CFG] AP SSID = "); DBG_PRINTLN(apSsid);
  DBG_PRINT("[CFG] AP IP   = "); DBG_PRINTLN(WiFi.softAPIP());
#endif

  server.on("/", HTTP_GET, handleRoot);
  server.on("/save", HTTP_POST, handleSave);
  server.begin();

  uint32_t enterMs = millis();
#if INKTIME_PHOTOPAINTER_ENABLED
  uint32_t lastPowerCheckMs = enterMs;
  bool usbServiceActive = photoPainter.usbConnected();
#endif

  for (;;) {
    server.handleClient();

#if INKTIME_PHOTOPAINTER_ENABLED
    if (millis() - lastPowerCheckMs >= 5000) {
      photoPainter.refreshPowerState();
      lastPowerCheckMs = millis();
      if (usbServiceActive && !photoPainter.usbConnected()) {
        goDeepSleepMinutes(minutesToNextRefreshFromLastEpoch(g_cfg));
      }
      usbServiceActive = photoPainter.usbConnected();
    }
#else
    const bool usbServiceActive = false;
#endif

    if (!usbServiceActive && millis() - enterMs > AP_TIMEOUT_MS) {
#if DEBUG_LOG
      DBG_PRINTLN("[AP] timeout: no config saved");
#endif
      uint32_t mins = minutesToNextRefreshFromLastEpoch(g_cfg);
#if DEBUG_LOG
      DBG_PRINT("[AP] sleep to next refresh, minutes="); DBG_PRINTLN((int)mins);
#endif
      delay(50);
      goDeepSleepMinutes(mins);
    }

    delay(10);
  }
}

#if INKTIME_PHOTOPAINTER_ENABLED
// A confirmed USB source keeps the existing configuration WebServer available.
// The project has no MQTT client to migrate; battery operation remains one-shot.
bool runUsbServiceMode() {
  photoPainter.refreshPowerState();
  if (!photoPainter.usbConnected()) return false;

  server.on("/", HTTP_GET, handleRoot);
  server.on("/save", HTTP_POST, handleSave);
  server.begin();
#if DEBUG_LOG
  DBG_PRINTLN("[USB] configuration WebServer remains awake until VBUS removal");
#endif

  uint32_t lastPowerCheckMs = millis();
  while (photoPainter.usbConnected()) {
    server.handleClient();
    if (millis() - lastPowerCheckMs >= 5000) {
      photoPainter.refreshPowerState();
      lastPowerCheckMs = millis();
    }
    delay(10);
  }
  server.stop();
  return true;
}
#endif

// =======================
//  WiFi 连接
// =======================
bool connectWiFi(const Config &cfg, uint32_t timeout_ms = 15000) {
#if DEBUG_LOG
  DBG_PRINTLN("[WIFI] connectWiFi()");
  DBG_PRINT("[WIFI] target ssid="); DBG_PRINTLN(cfg.wifi_ssid);
#endif

  if (cfg.wifi_ssid.isEmpty()) return false;

  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_STA);

  WiFi.setSleep(true);
  WiFi.setTxPower(WIFI_POWER_8_5dBm);
  WiFi.begin(cfg.wifi_ssid.c_str(), cfg.wifi_pass.c_str());

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < timeout_ms) {
    delay(200);
#if DEBUG_LOG
    DBG_PRINT(".");
#endif
  }
#if DEBUG_LOG
  DBG_PRINTLN();
#endif

  bool ok = (WiFi.status() == WL_CONNECTED);

#if DEBUG_LOG
  if (ok) {
    DBG_PRINTLN("[WIFI] connected");
    DBG_PRINT("[WIFI] IP="); DBG_PRINTLN(WiFi.localIP());
  } else {
    DBG_PRINTLN("[WIFI] connect FAILED");
  }
#endif

  return ok;
}

// =======================
//  NTP 同步时间
// =======================
bool syncTime(const Config &cfg, struct tm &outLocal) {
#if DEBUG_LOG
  DBG_PRINTLN("[TIME] syncTime start");
#endif
  long offsetSec = (long)cfg.tz_offset_minutes * 60;
  configTime(offsetSec, 0, "pool.ntp.org", "time.nist.gov", "ntp.aliyun.com");

  for (int i = 0; i < 30; ++i) {
    if (getLocalTime(&outLocal)) {
#if DEBUG_LOG
      char buf[64];
      strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &outLocal);
      DBG_PRINT("[TIME] OK: "); DBG_PRINTLN(buf);
#endif
      time_t nowEpoch = time(nullptr);
      if (nowEpoch > 0) {
        saveLastTimeEpoch(nowEpoch);
#if INKTIME_PHOTOPAINTER_ENABLED
        photoPainter.writeRtc(nowEpoch);
#endif
      }
      return true;
    }
    delay(500);
  }
#if DEBUG_LOG
  DBG_PRINTLN("[TIME] syncTime FAILED");
#endif
#if INKTIME_PHOTOPAINTER_ENABLED
  time_t rtcEpoch = 0;
  if (photoPainter.readRtc(rtcEpoch)) {
    struct timeval value = {rtcEpoch, 0};
    settimeofday(&value, nullptr);
    localtime_r(&rtcEpoch, &outLocal);
    saveLastTimeEpoch(rtcEpoch);
#if DEBUG_LOG
    DBG_PRINTLN("[TIME] restored from PCF85063");
#endif
    return true;
  }
#endif
  return false;
}

// =======================
//  下载每日相册 BIN
// =======================
bool downloadDailyPhotoBin(Config &cfg) {
  lastDeviceErrorCode = "";
  lastDeviceErrorMessage = "";
  const size_t pixelCount = (size_t)FB_WIDTH * FB_HEIGHT;

#if INKTIME_PHOTOPAINTER_ENABLED
  if (!photoPainter.hardwareReady()) {
    lastDeviceErrorCode = photoPainter.lastError();
    lastDeviceErrorMessage = "PhotoPainter Flash／OPI PSRAM 尚未就緒";
    return false;
  }
#endif

  if (cfg.backend_hostport.length() == 0 || cfg.device_token.length() == 0) {
#if DEBUG_LOG
    DBG_PRINTLN("[HTTP] 伺服器或裝置 Token 尚未設定，跳過下載");
#endif
    lastDeviceErrorCode = "DEVICE-CONFIG";
    lastDeviceErrorMessage = "伺服器或裝置 Token 尚未設定";
    return false;
  }

  String base = cfg.backend_hostport;
  base.trim();
  if (!base.startsWith("http://") && !base.startsWith("https://")) base = "http://" + base;
  while (base.endsWith("/")) base.remove(base.length() - 1);
  if (!backendTransportAllowed(base)) return false;
  String manifestUrl = base + String(DEVICE_MANIFEST_PATH);

#if DEBUG_LOG
  DBG_PRINTLN("[HTTP] 取得版本 Manifest（Authorization 已遮蔽）");
#endif

  HTTPClient manifestHttp;
  manifestHttp.setConnectTimeout(10000);
  manifestHttp.setTimeout(30000);
  const char* manifestHeaders[] = {"Content-Type"};
  manifestHttp.collectHeaders(manifestHeaders, 1);
  if (!manifestHttp.begin(manifestUrl)) {
    lastDeviceErrorCode = "DEVICE-MANIFEST-URL";
    lastDeviceErrorMessage = "Manifest URL 無法初始化";
    return false;
  }
  manifestHttp.addHeader("Authorization", "Bearer " + cfg.device_token);
  int manifestCode = manifestHttp.GET();
  const int manifestLength = manifestHttp.getSize();
  const String manifestContentType = manifestHttp.header("Content-Type");
  if (manifestCode != HTTP_CODE_OK || manifestLength <= 0 || manifestLength > 65536
      || !manifestContentType.startsWith("application/json")) {
#if DEBUG_LOG
    DBG_PRINT("[HTTP] Manifest code="); DBG_PRINTLN(manifestCode);
#endif
    manifestHttp.end();
    lastDeviceErrorCode = "DEVICE-MANIFEST-HTTP";
    lastDeviceErrorMessage = "Manifest HTTP／Content-Type／長度不合法";
    return false;
  }

  JsonDocument manifest;
  DeserializationError jsonError = deserializeJson(manifest, manifestHttp.getStream());
  manifestHttp.end();
  int schemaVersion = manifest["schema_version"] | 0;
  String pixelFormat = manifest["pixel_format"] | "";
  if (jsonError || (schemaVersion != 1 && schemaVersion != 2)
      || (pixelFormat != "2bpp" && pixelFormat != "indexed4")) {
#if DEBUG_LOG
    DBG_PRINTLN("[HTTP] Manifest 格式或版本不相容");
#endif
    lastDeviceErrorCode = "DEVICE-MANIFEST";
    lastDeviceErrorMessage = "Manifest 格式或版本不相容";
    return false;
  }

  // 伺服器端裝置頁是排程、時區與旋轉的正式來源；AP 值只在首次離線時備援。
  JsonObject remoteConfig = manifest["device_config"].as<JsonObject>();
  if (!remoteConfig.isNull()
      && (remoteConfig["schema_version"].as<int>() == 1
          || remoteConfig["schema_version"].as<int>() == 2)) {
    int offsetMinutes = remoteConfig["utc_offset_minutes"] | cfg.tz_offset_minutes;
    String schedule = remoteConfig["schedule"] | "";
    int separator = schedule.indexOf(':');
    int remoteHour = separator > 0 ? schedule.substring(0, separator).toInt() : -1;
    int remoteMinute = separator > 0 ? schedule.substring(separator + 1).toInt() : -1;
    int rotation = remoteConfig["rotation"] | (cfg.rotate180 ? 180 : 0);
    uint32_t desiredConfigVersion = remoteConfig["config_version"] | cfg.config_version;
    String desiredPanelProfile = remoteConfig["panel_profile"] | "safe_4c";
    bool compatiblePanel = desiredPanelProfile == "safe_4c"
      || desiredPanelProfile == String(INKTIME_PANEL_PROFILE);
    bool validRemote = offsetMinutes >= -12 * 60 && offsetMinutes <= 14 * 60
      && remoteHour >= 0 && remoteHour <= 23 && remoteMinute >= 0 && remoteMinute <= 59
      && (rotation == 0 || rotation == 180) && compatiblePanel
      && desiredConfigVersion >= cfg.config_version;
    if (!validRemote) {
      lastDeviceErrorCode = "DEVICE-CONFIG-PROFILE";
      lastDeviceErrorMessage = "遠端設定版本或面板 Profile 與韌體不相容";
      return false;
    }
    if (
        cfg.tz_offset_minutes != offsetMinutes || cfg.refresh_hour != remoteHour
        || cfg.refresh_minute != remoteMinute || cfg.rotate180 != (rotation == 180)
        || cfg.config_version != desiredConfigVersion) {
      cfg.tz_offset_minutes = offsetMinutes;
      cfg.refresh_hour = (uint8_t)remoteHour;
      cfg.refresh_minute = (uint8_t)remoteMinute;
      cfg.rotate180 = rotation == 180;
      cfg.config_version = desiredConfigVersion;
      saveConfig(cfg);
      serverConfigChanged = true;
#if DEBUG_LOG
      DBG_PRINTLN("[CFG] 已套用伺服器端裝置設定");
#endif
    }
  }

  int width = manifest["width"] | 0;
  int height = manifest["height"] | 0;
  JsonArray files = manifest["files"].as<JsonArray>();
  const char* downloadBaseRaw = manifest["download_base_url"] | "";
  String renderProfile = manifest["render_profile"] | "safe_4c";
  bool compatibleRenderProfile = renderProfile == "safe_4c"
    || renderProfile == String(INKTIME_PANEL_PROFILE);
  if (width != FB_WIDTH || height != FB_HEIGHT || files.size() == 0
      || strlen(downloadBaseRaw) == 0 || !compatibleRenderProfile) {
    lastDeviceErrorCode = "DEVICE-DISPLAY-MISMATCH";
    lastDeviceErrorMessage = "發布尺寸、Profile、檔案或下載路徑不相容";
    return false;
  }

  bool indexed4 = pixelFormat == "indexed4";
  size_t packedSize = pixelCount / (indexed4 ? 2 : 4);

  uint8_t* packed = nullptr;
#if INKTIME_PHOTOPAINTER_ENABLED
  packed = photoPainter.allocateWireBuffer(packedSize);
#else
  packed = (uint8_t*)heap_caps_malloc(packedSize, MALLOC_CAP_8BIT | MALLOC_CAP_SPIRAM);
  if (!packed) packed = (uint8_t*)heap_caps_malloc(packedSize, MALLOC_CAP_8BIT);
#endif
  if (!packed) {
    lastDeviceErrorCode = "DEVICE-MEMORY";
    lastDeviceErrorMessage = "無法配置下載緩衝區";
    return false;
  }

  // 隨機起點；若某張下載或校驗失敗，依序嘗試 Manifest 中其他照片。
  size_t startIndex = (size_t)random(0, files.size());
#if INKTIME_PHOTOPAINTER_ENABLED
  if (photoPainter.wokeFromUserButton()) {
    size_t previousIndex = 0;
    const bool hasPrevious = loadLastPhotoIndex(files.size(), previousIndex);
    startIndex = photoPainter.forceNetworkRefresh()
      ? (hasPrevious ? previousIndex : 0)
      : (hasPrevious ? (previousIndex + 1U) % files.size() : 0);
  }
#endif
  for (size_t attempt = 0; attempt < files.size(); ++attempt) {
    const size_t fileIndex = (startIndex + attempt) % files.size();
    JsonObject file = files[fileIndex];
    String fileName = file["name"] | "";
    size_t expectedSize = file["size"] | 0;
    String expectedSha = file["sha256"] | "";
    if (fileName.length() == 0 || expectedSize != packedSize
        || !inktime::isSha256Hex(expectedSha.c_str())) continue;

#if INKTIME_PHOTOPAINTER_ENABLED
    const uint32_t sourceHash = inktime::sourceHash32(expectedSha.c_str());
    const inktime::DisplayRotation rotation = cfg.rotate180
      ? inktime::DisplayRotation::Rotate180
      : inktime::DisplayRotation::Rotate0;
    uint8_t* cachedFrame = nullptr;
    if (photoPainter.loadCachedFrame(sourceHash, rotation, &cachedFrame)) {
      heap_caps_free(packed);
      if (frameData) heap_caps_free(frameData);
      frameData = cachedFrame;
      frameDataSize = inktime::kPhotoPainterFrameBytes;
      frameIndexed4 = true;
      frameNativePalette = true;
      currentReleaseId = manifest["release_id"] | "";
      currentRenderProfile = renderProfile;
      saveLastPhotoIndex(fileIndex);
      return true;
    }
#endif

    String fileUrl = base + String(downloadBaseRaw) + fileName;
    HTTPClient fileHttp;
    fileHttp.setConnectTimeout(10000);
    fileHttp.setTimeout(60000);
    const char* fileHeaders[] = {"Content-Type"};
    fileHttp.collectHeaders(fileHeaders, 1);
    if (!fileHttp.begin(fileUrl)) continue;
    fileHttp.addHeader("Authorization", "Bearer " + cfg.device_token);
    int code = fileHttp.GET();
    const String fileContentType = fileHttp.header("Content-Type");
    if (code != HTTP_CODE_OK || fileHttp.getSize() != (int)packedSize
        || !fileContentType.startsWith("application/octet-stream")) {
      fileHttp.end();
      continue;
    }

    WiFiClient *stream = fileHttp.getStreamPtr();
    size_t total = 0;
    uint32_t started = millis();
    while (total < packedSize && millis() - started < 60000) {
      size_t available = stream->available();
      if (!available) {
        if (!fileHttp.connected()) break;
        delay(1);
        continue;
      }
      size_t count = min(available, packedSize - total);
      int received = stream->read(packed + total, count);
      if (received > 0) total += received;
    }
    fileHttp.end();
    if (total != packedSize) continue;

    unsigned char digest[32];
    if (calculateSha256(packed, packedSize, digest) != 0) continue;
    char actualSha[65];
    for (int i = 0; i < 32; ++i) sprintf(actualSha + i * 2, "%02x", digest[i]);
    actualSha[64] = '\0';
    if (!expectedSha.equalsIgnoreCase(String(actualSha))) continue;

    // 完整下載與 SHA-256 都通過後才替換資料。
    if (frameData) heap_caps_free(frameData);
#if INKTIME_PHOTOPAINTER_ENABLED
    uint8_t* nativeFrame = nullptr;
    if (!photoPainter.convertAndCache(
          packed,
          packedSize,
          indexed4,
          sourceHash,
          rotation,
          &nativeFrame)) {
      frameData = nullptr;
      lastDeviceErrorCode = photoPainter.lastError();
      lastDeviceErrorMessage = "PhotoPainter framebuffer 轉換失敗";
      continue;
    }
    heap_caps_free(packed);
    frameData = nativeFrame;
    frameDataSize = inktime::kPhotoPainterFrameBytes;
    frameIndexed4 = true;
    frameNativePalette = true;
#else
    frameData = packed;
    frameDataSize = packedSize;
    frameIndexed4 = indexed4;
    frameNativePalette = false;
#endif
    currentReleaseId = manifest["release_id"] | "";
    currentRenderProfile = renderProfile;
#if INKTIME_PHOTOPAINTER_ENABLED
    saveLastPhotoIndex(fileIndex);
#endif
    return true;
  }

  heap_caps_free(packed);
  lastDeviceErrorCode = "DEVICE-DOWNLOAD";
  lastDeviceErrorMessage = "所有發布檔案下載或 SHA-256 校驗失敗";
  return false;
}

void reportDeviceStatus(const Config &cfg, bool displayUpdated) {
  if (WiFi.status() != WL_CONNECTED || cfg.backend_hostport.length() == 0 || cfg.device_token.length() == 0) return;
  String base = cfg.backend_hostport;
  base.trim();
  if (!base.startsWith("http://") && !base.startsWith("https://")) base = "http://" + base;
  while (base.endsWith("/")) base.remove(base.length() - 1);
  if (!backendTransportAllowed(base)) return;

#if INKTIME_PHOTOPAINTER_ENABLED
  photoPainter.readEnvironment();
#endif
  JsonDocument payload;
  payload["firmware_version"] = INKTIME_FIRMWARE_VERSION;
  payload["board_profile"] = kBoardConfig.name;
  payload["wifi_rssi"] = WiFi.RSSI();
  payload["free_heap_bytes"] = ESP.getFreeHeap();
  payload["free_psram_bytes"] = ESP.getFreePsram();
  payload["wake_reason"] = String((int)esp_sleep_get_wakeup_cause());
  payload["display_updated"] = displayUpdated;
  payload["applied_config_version"] = cfg.config_version;
  payload["panel_profile"] = INKTIME_PANEL_PROFILE;
  payload["render_profile"] = currentRenderProfile;
  payload["release_id"] = currentReleaseId;
  payload["error_code"] = lastDeviceErrorCode;
  payload["error_message"] = lastDeviceErrorMessage;
#if INKTIME_PHOTOPAINTER_ENABLED
  payload["flash_bytes"] = ESP.getFlashChipSize();
  payload["psram_bytes"] = ESP.getPsramSize();
  payload["flash_ready"] = photoPainter.flashReady();
  payload["psram_ready"] = photoPainter.psramReady();
  payload["sd_card"] = photoPainter.sdReady();
  payload["rtc"] = photoPainter.rtcReady();
  payload["cache_status"] = inktime::cacheStatusName(photoPainter.cacheStatus());
  payload["pmic_type"] = inktime::pmicTypeName(photoPainter.pmicType());
  payload["usb_power"] = photoPainter.usbConnected();
  if (photoPainter.batteryVoltage() > 0.0f) {
    payload["battery_voltage"] = photoPainter.batteryVoltage();
  }
  if (photoPainter.batteryPercent() >= 0) {
    payload["battery_percent"] = photoPainter.batteryPercent();
    payload["battery_percent_estimated"] = true;
  }
  if (photoPainter.environmentValid()) {
    payload["temperature_c"] = photoPainter.temperatureC();
    payload["humidity_percent"] = photoPainter.humidityPercent();
  }
  payload["last_refresh_duration_ms"] = photoPainter.lastRefreshDurationMs();
  payload["button_wakeup"] = photoPainter.wokeFromUserButton();
#endif
  String body;
  serializeJson(payload, body);

  HTTPClient statusHttp;
  statusHttp.setConnectTimeout(10000);
  statusHttp.setTimeout(15000);
  if (!statusHttp.begin(base + String(DEVICE_STATUS_PATH))) return;
  statusHttp.addHeader("Authorization", "Bearer " + cfg.device_token);
  statusHttp.addHeader("Content-Type", "application/json");
  statusHttp.POST(body);
  statusHttp.end();
}

// =======================
//  墨水屏显示
// =======================
void initDisplay(const Config &cfg) {
#if INKTIME_PHOTOPAINTER_ENABLED
  (void)cfg;
#else
#if DEBUG_LOG
  DBG_PRINTLN("[EPD] initDisplay");
#endif
  SPI.end();
  SPI.begin(
    kBoardConfig.display.spi.sck,
    kBoardConfig.display.spi.miso,
    kBoardConfig.display.spi.mosi,
    kBoardConfig.display.spi.cs
  );

  display.init(0, true, 2, false);

  if (cfg.rotate180) display.setRotation(3);
  else              display.setRotation(1);
#endif
}

bool drawFromFrameData(const Config &cfg) {
  (void)cfg;

#if INKTIME_PHOTOPAINTER_ENABLED
  if (!frameNativePalette || frameDataSize != inktime::kPhotoPainterFrameBytes) return false;
  return photoPainter.displayFrame(frameData, frameDataSize);
#else

  display.setFullWindow();
  int w = display.width();   // 480
  int h = display.height();  // 800

#if DEBUG_LOG
  DBG_PRINT("[EPD] logical w="); DBG_PRINT(w);
  DBG_PRINT(" h="); DBG_PRINTLN(h);
#endif

  display.firstPage();
  do {
    for (int y = 0; y < FB_HEIGHT && y < h; ++y) {
      for (int x = 0; x < FB_WIDTH && x < w; ++x) {
        size_t pixel = (size_t)y * FB_WIDTH + x;
        uint8_t packed = frameData[pixel / (frameIndexed4 ? 2 : 4)];
        uint8_t c = frameIndexed4
          ? ((pixel % 2 == 0) ? (packed >> 4) : (packed & 0x0F))
          : ((packed >> (6 - (pixel % 4) * 2)) & 0x03);
        uint16_t col;
        if (frameIndexed4) {
          switch (c) {
            case 0: col = GxEPD_BLACK;  break;
            case 1: col = GxEPD_WHITE;  break;
            case 2: col = GxEPD_GREEN;  break;
            case 3: col = GxEPD_BLUE;   break;
            case 4: col = GxEPD_RED;    break;
            case 5: col = GxEPD_YELLOW; break;
            case 6: col = GxEPD_ORANGE; break;
            default: col = GxEPD_WHITE; break;
          }
        } else {
          switch (c) {
            case 0: col = GxEPD_BLACK;  break;
            case 1: col = GxEPD_WHITE;  break;
            case 2: col = GxEPD_RED;    break;
            case 3: col = GxEPD_YELLOW; break;
            default: col = GxEPD_WHITE; break;
          }
        }
        display.drawPixel(x, y, col);
      }
    }
  } while (display.nextPage());

  display.hibernate();
  return true;
#endif
}

// =======================
//  睡到下一个唤醒点
// =======================
void sleepUntilNextSchedule(const Config &cfg, bool hasTime, const struct tm &now) {
  if (!hasTime) {
    goDeepSleepMinutes(1440);
    return;
  }

  int curMinOfDay = now.tm_hour * 60 + now.tm_min;
  int targetMin   = (int)cfg.refresh_hour * 60 + (int)cfg.refresh_minute;
  int delta;

  if (curMinOfDay < targetMin) delta = targetMin - curMinOfDay;
  else                         delta = 24 * 60 - (curMinOfDay - targetMin);

  if (delta < 1) delta = 24 * 60;

#if DEBUG_LOG
  DBG_PRINT("[SLEEP] nowMin="); DBG_PRINT(curMinOfDay);
  DBG_PRINT(" targetMin="); DBG_PRINT(targetMin);
  DBG_PRINT(" delta="); DBG_PRINTLN(delta);
#endif

  goDeepSleepMinutes((uint32_t)delta);
}

// =======================
//  setup / loop
// =======================
void setup() {
  releaseAllGpioHoldsAtBoot();

  setCpuFrequencyMhz(80);
  if (kBoardConfig.statusLed != inktime::kNoPin) {
    pinMode(kBoardConfig.statusLed, OUTPUT);
    digitalWrite(kBoardConfig.statusLed, LOW);
  }

  DBG_BEGIN();
  delay(200);

#if DEBUG_LOG
  DBG_PRINTLN();
  DBG_PRINTLN("===== ESP32-S3 InkTime Daily Photo boot =====");
#endif

  if (isFactoryResetRequestedAtBoot()) {
#if DEBUG_LOG
  DBG_PRINT("[BOOT] factory reset GPIO=");
  DBG_PRINTLN((int)kBoardConfig.buttons.factoryReset);
#endif
  clearConfigNVS();

  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);
  esp_wifi_stop();
  delay(200);
}

  randomSeed(esp_random());

#if INKTIME_PHOTOPAINTER_ENABLED
  if (!photoPainter.begin()) {
    lastDeviceErrorCode = photoPainter.lastError();
    lastDeviceErrorMessage = "PhotoPainter Flash／OPI PSRAM 不存在或容量不足";
  }
#endif

  loadConfig(g_cfg);

  if (!g_cfg.valid) {
#if DEBUG_LOG
    DBG_PRINTLN("[BOOT] no valid config -> AP portal");
#endif
    startConfigPortal();
  }

#if DEBUG_LOG
  DBG_PRINTLN("[BOOT] have config -> connect WiFi");
#endif
  if (!connectWiFi(g_cfg)) {
#if DEBUG_LOG
    DBG_PRINTLN("[BOOT] connect failed");
#endif
#if INKTIME_PHOTOPAINTER_ENABLED
    // Known battery power must not remain in a network retry/configuration loop.
    // USB or an unidentified PMIC keeps the bounded AP diagnostics path available.
    if (photoPainter.powerSourceKnown() && !photoPainter.usbConnected()) {
      long offsetSec = (long)g_cfg.tz_offset_minutes * 60;
      configTime(offsetSec, 0, "pool.ntp.org");
      time_t rtcEpoch = 0;
      struct tm offlineTime = {};
      bool hasOfflineTime = photoPainter.readRtc(rtcEpoch);
      if (hasOfflineTime) {
        struct timeval value = {rtcEpoch, 0};
        settimeofday(&value, nullptr);
        localtime_r(&rtcEpoch, &offlineTime);
      }
      lastDeviceErrorCode = "DEVICE-WIFI-TIMEOUT";
      lastDeviceErrorMessage = "電池模式 Wi-Fi 逾時，已停止重試";
      sleepUntilNextSchedule(g_cfg, hasOfflineTime, offlineTime);
    }
#endif
    DBG_PRINTLN("[BOOT] enter bounded AP portal");
    startConfigPortal();
  }

  struct tm timeinfo;
  bool hasTime = syncTime(g_cfg, timeinfo);

  bool ok = downloadDailyPhotoBin(g_cfg);
  if (serverConfigChanged) hasTime = syncTime(g_cfg, timeinfo);
  bool displayUpdated = false;
  if (ok) {
    initDisplay(g_cfg);
    displayUpdated = drawFromFrameData(g_cfg);
    if (!displayUpdated) {
#if INKTIME_PHOTOPAINTER_ENABLED
      lastDeviceErrorCode = photoPainter.lastError();
#else
      lastDeviceErrorCode = "DEVICE-DISPLAY";
#endif
      lastDeviceErrorMessage = "電子紙刷新失敗或逾時";
    }
  } else {
#if DEBUG_LOG
    DBG_PRINTLN("[BOOT] downloadDailyPhotoBin FAILED");
#endif
  }
  reportDeviceStatus(g_cfg, displayUpdated);

#if INKTIME_PHOTOPAINTER_ENABLED
  if (runUsbServiceMode()) {
    // The prior timestamp may be hours old after a USB service session.
    hasTime = getLocalTime(&timeinfo, 1000);
  }
#endif

  if (!hasTime) {
    struct tm tmp;
    if (syncTime(g_cfg, tmp)) sleepUntilNextSchedule(g_cfg, true, tmp);
    else                      sleepUntilNextSchedule(g_cfg, false, timeinfo);
  } else {
    sleepUntilNextSchedule(g_cfg, true, timeinfo);
  }
}

void loop() {
}
