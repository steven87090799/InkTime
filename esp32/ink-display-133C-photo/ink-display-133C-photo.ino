#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <time.h>
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_bt.h"
#include "driver/gpio.h"
#include "driver/rtc_io.h"

#include <SPI.h>
#include "epd.h"   // ✅ 走你已经跑通的驱动链路

// =======================
//  调试开关
// =======================
#ifndef INKTIME_DEBUG_LOG
#define INKTIME_DEBUG_LOG 0
#endif
#define DEBUG_LOG INKTIME_DEBUG_LOG

HardwareSerial DebugSerial(0);
#include "firmware_observability.h"

#if DEBUG_LOG
  #define DBG_BEGIN()    INK_LOG_BEGIN()
  #define DBG_PRINT(x)   DebugSerial.print(x)
  #define DBG_PRINTLN(x) DebugSerial.println(x)
#else
  #define DBG_BEGIN()    INK_LOG_BEGIN()
  #define DBG_PRINT(x)
  #define DBG_PRINTLN(x)
#endif

#ifndef LED_BUILTIN
#define LED_BUILTIN 2
#endif

// =======================
//  恢复出厂设置：上电时按下 GPIO35 -> 清 NVS 中的 WiFi/配置，并进入 AP 配网
// =======================
#define PIN_FACTORY_RESET 35
#define FACTORY_RESET_ACTIVE_LOW 1
static const uint32_t FACTORY_RESET_SAMPLE_DELAY_MS = 5;

// =======================
//  AP 配置页保底：进入 AP 后 5 分钟没保存配置 -> 睡到“下一个刷新点”
// =======================
static const uint32_t AP_TIMEOUT_MS = 5UL * 60UL * 1000UL; // 5 分钟

// =======================
//  13.3E6 参数（1200x1600，4bpp）
// =======================
static const int FB_WIDTH   = 1200;
static const int FB_HEIGHT  = 1600;
static const size_t FB_BYTES  = (size_t)FB_WIDTH * (size_t)FB_HEIGHT / 2; // 960000
static const size_t HALF_BYTES = FB_BYTES / 2; // 480000

// 每行 1200 像素，4bpp packed => 600 bytes
static const size_t ROW_BYTES_TOTAL = (size_t)FB_WIDTH / 2;   // 600
static const size_t ROW_BYTES_HALF  = ROW_BYTES_TOTAL / 2;    // 300

// 你板子文档的 IO（跟 demo 一致）
#define PIN_EPD_BUSY   25
#define PIN_EPD_RST    26
#define PIN_EPD_DC     27
#define PIN_EPD_CS_S   2
#define PIN_EPD_CS_M   15
#define PIN_EPD_SCLK   13
#define PIN_EPD_MOSI   14
#define PIN_EPD_PWR_ON 32

// Some vendor EPD libs use PIN_SPI_CS_M / PIN_SPI_CS_S macros.
#ifndef PIN_SPI_CS_M
#define PIN_SPI_CS_M PIN_EPD_CS_M
#endif
#ifndef PIN_SPI_CS_S
#define PIN_SPI_CS_S PIN_EPD_CS_S
#endif

// =======================
//  静态每日相册 BIN 路径前缀（13.3 版本）
//  你服务端输出：photo_13in3_6c_{idx}_L.bin / _R.bin
// =======================
#define DAILY_PHOTO_PATH_PREFIX "/static/inktime/yourdownloadkey/photo_13in3_6c_"
#define DAILY_PHOTO_L_SUFFIX    "_L.bin"
#define DAILY_PHOTO_R_SUFFIX    "_R.bin"
#define DAILY_PHOTO_COUNT       5   // 0..4

// =======================
//  配置存储 / WiFi / WebServer
// =======================
Preferences prefs;
WebServer  server(80);

struct Config {
  String  wifi_ssid;
  String  wifi_pass;
  String  backend_hostport;
  int32_t tz_offset_hours;
  uint8_t refresh_hour;
  bool    rotate180; // 保留字段（此版不做旋转）
  bool    valid;
};

const char*  DEFAULT_HOSTPORT = "";
const int32_t DEFAULT_TZ      = 8;
const uint8_t DEFAULT_HOUR    = 8;

Config g_cfg;

// =======================
//  解除 DeepSleep hold
// =======================
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
  // GPIO35 is input-only and has NO internal pull-up/down.
  // Use an external pull-up/down resistor on the board for reliable factory reset.
  pinMode(PIN_FACTORY_RESET, INPUT);
  delay(FACTORY_RESET_SAMPLE_DELAY_MS);
#if FACTORY_RESET_ACTIVE_LOW
  return (digitalRead(PIN_FACTORY_RESET) == LOW);
#else
  return (digitalRead(PIN_FACTORY_RESET) == HIGH);
#endif
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

static uint32_t minutesToNextRefreshFromLastEpoch(const Config &cfg) {
  time_t lastEpoch;
  if (!loadLastTimeEpoch(lastEpoch)) return 1440;

  struct tm t;
  localtime_r(&lastEpoch, &t);

  int curMinOfDay = t.tm_hour * 60 + t.tm_min;
  int targetMin   = (int)cfg.refresh_hour * 60;
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
  prefs.begin("dashcfg", true);
  cfg.wifi_ssid        = prefs.getString("ssid", "");
  cfg.wifi_pass        = prefs.getString("pass", "");
  cfg.backend_hostport = prefs.getString("hostport", DEFAULT_HOSTPORT);
  cfg.tz_offset_hours  = prefs.getInt("tz", DEFAULT_TZ);
  cfg.refresh_hour     = (uint8_t)prefs.getUChar("hour", DEFAULT_HOUR);
  cfg.rotate180        = prefs.getBool("rot180", false);
  prefs.end();

  cfg.valid = (cfg.wifi_ssid.length() > 0);

#if DEBUG_LOG
  DBG_PRINTLN("---- loadConfig ----");
  DBG_PRINTLN("[CFG] WiFi SSID configured (value redacted)");
  DBG_PRINTLN("[CFG] backend endpoint configured (value redacted)");
  DBG_PRINT("[CFG] tz_offset_hours="); DBG_PRINTLN(cfg.tz_offset_hours);
  DBG_PRINT("[CFG] refresh_hour="); DBG_PRINTLN((int)cfg.refresh_hour);
  DBG_PRINT("[CFG] rotate180="); DBG_PRINTLN(cfg.rotate180 ? "true" : "false");
  DBG_PRINT("[CFG] valid="); DBG_PRINTLN(cfg.valid ? "true" : "false");
#endif
}

void saveConfig(const Config &cfg) {
  prefs.begin("dashcfg", false);
  prefs.putString("ssid", cfg.wifi_ssid);
  prefs.putString("pass", cfg.wifi_pass);
  prefs.putString("hostport", cfg.backend_hostport);
  prefs.putInt("tz", cfg.tz_offset_hours);
  prefs.putUChar("hour", cfg.refresh_hour);
  prefs.putBool("rot180", cfg.rotate180);
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

  int n = WiFi.scanNetworks(false, true);

#if DEBUG_LOG
  DBG_PRINT("[CFG] scanNetworks n="); DBG_PRINTLN(n);
#endif

  String curSsid = g_cfg.wifi_ssid;
  String host    = htmlEscape(g_cfg.backend_hostport);
  int32_t tz     = g_cfg.tz_offset_hours;
  if (tz < -12 || tz > 14) tz = DEFAULT_TZ;
  uint8_t hour   = g_cfg.refresh_hour;
  if (hour > 23) hour = DEFAULT_HOUR;
  bool rot180    = g_cfg.rotate180;

  String html;
  html.reserve(4096);

  html += F("<!DOCTYPE html><html><head><meta charset='utf-8'>");
  html += F("<meta name='viewport' content='width=device-width,initial-scale=1'>");
  html += F("<title>InkTime 设置</title></head><body>");
  html += F("<h2>InkTime 设置</h2>");
  html += F("<form method='POST' action='/save'>");

  html += F("WiFi SSID:<br>");
  html += F("<select id='ssid_select' style='width: 288px;' onchange=\"document.getElementById('ssid_input').value=this.value;\">");
  html += F("<option value=''>（手动输入或选择）</option>");
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

  html += F("密码:<br><input name='pass' type='password' style='width: 280px;'><br><br>");

  html += F("服务器 (host:port):<br><input name='hostport' size='40' value='");
  html += host;
  html += F("'><br><br>");

  html += F("每日刷新时间（0-23 点整）：<br><select name='hour'>");
  for (int h = 0; h < 24; ++h) {
    html += "<option value='";
    html += String(h);
    html += "'";
    if (h == hour) html += " selected";
    html += ">";
    html += String(h);
    html += F(" 点</option>");
  }
  html += F("</select><br><br>");

  html += F("时区:<br><select name='tz'>");
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
  html += F("> 画面旋转 180°（此版暂不实现，仅保留配置字段）</label><br><br>");

  if (n <= 0) {
    html += F("<p style='color:#c00'>未扫描到 WiFi，可直接在上方输入框手动填写 SSID。</p>");
  }

  html += F("<input type='submit' value='保存并重启'>");
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
  String hourStr  = server.arg("hour");
  String tzStr    = server.arg("tz");
  bool rot180Req  = (server.arg("rot180") == "1");

  ssid.trim();
  host.trim();

  Config newCfg = g_cfg;

  if (ssid.length() > 0) newCfg.wifi_ssid = ssid;
  if (pass.length() > 0) newCfg.wifi_pass = pass;

  newCfg.backend_hostport = host;

  int32_t tz = tzStr.toInt();
  if (tz < -12) tz = -12;
  if (tz > 14)  tz = 14;
  newCfg.tz_offset_hours = tz;

  int hour = hourStr.toInt();
  if (hour < 0)  hour = 0;
  if (hour > 23) hour = 23;
  newCfg.refresh_hour = (uint8_t)hour;

  newCfg.rotate180 = rot180Req;
  newCfg.valid     = (newCfg.wifi_ssid.length() > 0);

  saveConfig(newCfg);

  server.send(200, "text/html; charset=utf-8",
              F("<html><body><h3>保存成功，设备即将重启...</h3></body></html>"));

  delay(800);
  ESP.restart();
}

// =======================
//  Deep Sleep 前
// =======================
void prepareDeepSleepDomains() {
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH,    ESP_PD_OPTION_OFF);
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_SLOW_MEM,  ESP_PD_OPTION_OFF);
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_FAST_MEM,  ESP_PD_OPTION_OFF);
}

// =======================
//  关闭墨水屏相关引脚
// =======================
static void powerDownEPD() {
  const int epdPins[] = {
    PIN_EPD_BUSY, PIN_EPD_RST, PIN_EPD_DC,
    PIN_EPD_CS_M, PIN_EPD_CS_S,
    PIN_EPD_SCLK, PIN_EPD_MOSI,
    PIN_EPD_PWR_ON
  };
  for (size_t i = 0; i < sizeof(epdPins)/sizeof(epdPins[0]); ++i) {
    int p = epdPins[i];
    pinMode(p, INPUT_PULLDOWN);
  }
}

static void deepSleepHoldOnlyEpdPins() {
  const int epdPins[] = {
    PIN_EPD_BUSY, PIN_EPD_RST, PIN_EPD_DC,
    PIN_EPD_CS_M, PIN_EPD_CS_S,
    PIN_EPD_SCLK, PIN_EPD_MOSI,
    PIN_EPD_PWR_ON
  };
  for (size_t i = 0; i < sizeof(epdPins)/sizeof(epdPins[0]); ++i) {
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

  String apSsid     = "InkTime-" + String((uint32_t)ESP.getEfuseMac(), HEX).substring(4);
  const char* apPwd = "12345678";

  bool apOk = WiFi.softAP(apSsid.c_str(), apPwd);

#if DEBUG_LOG
  DBG_PRINT("[CFG] softAP result = "); DBG_PRINTLN(apOk ? "OK" : "FAIL");
  DBG_PRINTLN("[CFG] provisioning AP started (SSID redacted)");
  DBG_PRINT("[CFG] AP IP   = "); DBG_PRINTLN(WiFi.softAPIP());
#endif

  server.on("/", HTTP_GET, handleRoot);
  server.on("/save", HTTP_POST, handleSave);
  server.begin();

  uint32_t enterMs = millis();

  for (;;) {
    server.handleClient();

    if (millis() - enterMs > AP_TIMEOUT_MS) {
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

// =======================
//  WiFi 连接
// =======================
bool connectWiFi(const Config &cfg, uint32_t timeout_ms = 15000) {
  INK_LOG_DEBUG("wifi_connect_started", "Wi-Fi connection attempt started");
#if DEBUG_LOG
  DBG_PRINTLN("[WIFI] connectWiFi()");
#endif

  if (cfg.wifi_ssid.isEmpty()) return false;

  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_STA);

  // 这里保持你原配置；如果你供电很紧张，反而建议 setSleep(false) + WIFI_PS_NONE
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
  if (ok) INK_LOG_INFO("wifi_connected", "Wi-Fi connected");
  else INK_LOG_WARN("wifi_connect_timeout", "Wi-Fi connection timed out");

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
  long offsetSec = (long)cfg.tz_offset_hours * 3600;
  configTime(offsetSec, 0, "pool.ntp.org", "time.nist.gov", "ntp.aliyun.com");

  for (int i = 0; i < 30; ++i) {
    if (getLocalTime(&outLocal)) {
#if DEBUG_LOG
      char buf[64];
      strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &outLocal);
      DBG_PRINT("[TIME] OK: "); DBG_PRINTLN(buf);
#endif
      time_t nowEpoch = time(nullptr);
      if (nowEpoch > 0) saveLastTimeEpoch(nowEpoch);
      return true;
    }
    delay(1000);
  }
#if DEBUG_LOG
  DBG_PRINTLN("[TIME] syncTime FAILED");
#endif
  return false;
}

// =======================
//  EPD init（加固：GPIO2 稳定、正确打印顺序、失败断电重试）
// =======================
static bool epdInitOnce() {
  // Ensure CS lines are deasserted early (GPIO2 is strapping on many boards)
  pinMode(PIN_EPD_CS_M, OUTPUT);
  digitalWrite(PIN_EPD_CS_M, HIGH);

  // For GPIO2 (CS_S), keep it pulled-up as early as possible to reduce strapping/LED interference.
  pinMode(PIN_EPD_CS_S, INPUT_PULLUP);
  delay(2);

  // Seller-required sequence (your validated minimal path)
  SPI.end();
  SPI.begin(PIN_EPD_SCLK, -1, PIN_EPD_MOSI, PIN_EPD_CS_M);

  pinMode(PIN_EPD_PWR_ON, OUTPUT);
  digitalWrite(PIN_EPD_PWR_ON, HIGH);
  delay(1000);

#if DEBUG_LOG
  DBG_PRINTLN("[EPD] External power enabled");
  DBG_PRINTLN("[EPD] EPD_initSPI() ...");
#endif

  EPD_initSPI();
  delay(50);

  // Now take control of CS_S as output and keep both high
  pinMode(PIN_EPD_CS_S, OUTPUT);
  digitalWrite(PIN_EPD_CS_S, HIGH);
  digitalWrite(PIN_EPD_CS_M, HIGH);
  delay(10);

  // Extra hard reset pulse
  pinMode(PIN_EPD_RST, OUTPUT);
  digitalWrite(PIN_EPD_RST, LOW);
  delay(20);
  digitalWrite(PIN_EPD_RST, HIGH);
  delay(120);

  pinMode(PIN_EPD_BUSY, INPUT);

#if DEBUG_LOG
  DBG_PRINT("[EPD] pre-init: CS_M="); DBG_PRINT(digitalRead(PIN_EPD_CS_M));
  DBG_PRINT(" CS_S="); DBG_PRINT(digitalRead(PIN_EPD_CS_S));
  DBG_PRINT(" BUSY="); DBG_PRINTLN(digitalRead(PIN_EPD_BUSY));
#endif

  EPD_dispIndex = 50; // 13.3E6

#if DEBUG_LOG
  DBG_PRINTLN("[EPD] EPD_dispInit() ...");
#endif

  EPD_dispInit();

  if (EPD_dispLoad == NULL) {
#if DEBUG_LOG
    DBG_PRINTLN("[EPD] ERROR: EPD_dispLoad is NULL");
#endif
    return false;
  }

  return true;
}

static void initEpd13in3e() {
  const int kRetries = 3;
  for (int i = 0; i < kRetries; ++i) {
#if DEBUG_LOG
    DBG_PRINT("[EPD] init try "); DBG_PRINT(i + 1); DBG_PRINT("/"); DBG_PRINTLN(kRetries);
#endif

    // Hard power cycle the panel
    pinMode(PIN_EPD_PWR_ON, OUTPUT);
    digitalWrite(PIN_EPD_PWR_ON, LOW);
    delay(200);

    if (epdInitOnce()) return;

    delay(200);
  }

#if DEBUG_LOG
  DBG_PRINTLN("[EPD] FATAL: init failed after retries");
#endif
  while (1) delay(1000);
}

// =======================
//  HTTP 流式：半屏 480000 bytes（300 bytes/行 * 1600 行）
//  - 直接按行喂给 EPD_dispLoad，不落盘
// =======================
static bool streamHttpHalfToEpd(const String& url, bool isLeftHalf) {
#if DEBUG_LOG
  DBG_PRINTLN("[HTTP] payload URL redacted");
#endif

  HTTPClient http;
  http.begin(url);
  http.setTimeout(20000);

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    INK_LOG_ERROR("payload_transport_failed", "Payload request returned a non-success status");
#if DEBUG_LOG
    DBG_PRINT("[HTTP] code=");
    DBG_PRINTLN(code);
#endif
    http.end();
    return false;
  }

  int len = http.getSize();
#if DEBUG_LOG
  DBG_PRINT("[HTTP] content-length=");
  DBG_PRINTLN(len);
#endif

  const int expected = (int)HALF_BYTES; // 480000
  if (len > 0 && len != expected) {
    INK_LOG_ERROR("payload_validation_failed", "Payload length did not match display contract");
#if DEBUG_LOG
    DBG_PRINT("[HTTP] length mismatch expect=");
    DBG_PRINT(expected);
    DBG_PRINT(" got=");
    DBG_PRINTLN(len);
#endif
    http.end();
    return false;
  }

  WiFiClient* stream = http.getStreamPtr();

  static uint8_t lineBuf[ROW_BYTES_HALF]; // 300B

  // 与已跑通 demo 一致：左半 CS_M 低，右半 CS_S 低
  EPD_CS_ALL(1);
  if (isLeftHalf) {
    digitalWrite(PIN_SPI_CS_M, 0);
    digitalWrite(PIN_SPI_CS_S, 1);
  } else {
    digitalWrite(PIN_SPI_CS_M, 1);
    digitalWrite(PIN_SPI_CS_S, 0);
  }
  EPD_SendCommand_13in3E6(0x10);

  size_t totalRead = 0;
  const uint32_t DOWNLOAD_TIMEOUT_MS = 240 * 1000;
  uint32_t startMs = millis();

  for (int y = 0; y < FB_HEIGHT; ++y) {
    size_t got = 0;
    while (got < ROW_BYTES_HALF) {
      if (millis() - startMs > DOWNLOAD_TIMEOUT_MS) {
        INK_LOG_ERROR("payload_transport_timeout", "Payload stream exceeded bounded deadline");
#if DEBUG_LOG
        DBG_PRINTLN("[HTTP] download timeout");
#endif
        http.end();
        return false;
      }

      int avail = stream->available();
      if (avail <= 0) { delay(1); continue; }

      size_t toRead = (size_t)avail;
      if (toRead > (ROW_BYTES_HALF - got)) toRead = (ROW_BYTES_HALF - got);

      int r = stream->read(lineBuf + got, (int)toRead);
      if (r > 0) {
        got += (size_t)r;
        totalRead += (size_t)r;
      }
    }

    EPD_dispLoad(lineBuf, (int)ROW_BYTES_HALF);

    if ((y & 0x0F) == 0) yield();

#if DEBUG_LOG
    if ((y % 200) == 0) {
      DBG_PRINT("[EPD] ");
      DBG_PRINT(isLeftHalf ? "L" : "R");
      DBG_PRINT(" y=");
      DBG_PRINT(y);
      DBG_PRINT("/");
      DBG_PRINTLN(FB_HEIGHT);
    }
#endif
  }

  http.end();

  if (len <= 0 && (int)totalRead != expected) {
#if DEBUG_LOG
    DBG_PRINT("[HTTP] size mismatch expect=");
    DBG_PRINT(expected);
    DBG_PRINT(" got=");
    DBG_PRINTLN((int)totalRead);
#endif
    return false;
  }

  return true;
}

// =======================
//  下载并显示（组 URL + 随机 idx）
// =======================
static bool downloadAndShowDaily(const Config &cfg) {
  if (cfg.backend_hostport.length() == 0) {
#if DEBUG_LOG
    DBG_PRINTLN("[HTTP] hostport empty, skip");
#endif
    return false;
  }

  int idx = random(0, DAILY_PHOTO_COUNT);

  String hp = cfg.backend_hostport;
  hp.trim();

  String base;
  if (hp.startsWith("http://") || hp.startsWith("https://")) base = hp;
  else base = "http://" + hp;

  // /static/inktime/<key>/photo_13in3_6c_3_L.bin
  String urlL = base + String(DAILY_PHOTO_PATH_PREFIX) + String(idx) + String(DAILY_PHOTO_L_SUFFIX);
  String urlR = base + String(DAILY_PHOTO_PATH_PREFIX) + String(idx) + String(DAILY_PHOTO_R_SUFFIX);

#if DEBUG_LOG
  DBG_PRINT("[HTTP] idx="); DBG_PRINTLN(idx);
#endif

  initEpd13in3e();
  INK_LOG_INFO("payload_download_started", "Bounded display payload download started");

#if DEBUG_LOG
  DBG_PRINTLN("[EPD] Streaming LEFT half (CS_M) ...");
#endif
  if (!streamHttpHalfToEpd(urlL, true)) {
    INK_LOG_ERROR("payload_download_failed", "Left display payload failed validation");
#if DEBUG_LOG
    DBG_PRINTLN("[EPD] LEFT half download/stream FAILED");
#endif
    return false;
  }

#if DEBUG_LOG
  DBG_PRINTLN("[EPD] Streaming RIGHT half (CS_S) ...");
#endif
  if (!streamHttpHalfToEpd(urlR, false)) {
    INK_LOG_ERROR("payload_download_failed", "Right display payload failed validation");
#if DEBUG_LOG
    DBG_PRINTLN("[EPD] RIGHT half download/stream FAILED");
#endif
    return false;
  }

#if DEBUG_LOG
  DBG_PRINTLN("[EPD] SHOW (refresh) ...");
#endif

  INK_LOG_INFO("payload_download_completed", "Display payload passed bounded validation");
  INK_LOG_INFO("display_refresh_started", "Electronic paper refresh started");

  // Shut down WiFi/BT before refresh to reduce power/EMI spikes during waveform drive
  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);
  esp_wifi_stop();
#if defined(CONFIG_BT_ENABLED)
  esp_bt_controller_disable();
#endif
  delay(200);

  EPD_dispMass[EPD_dispIndex].show();
  INK_LOG_INFO("display_refresh_completed", "Electronic paper refresh completed");

#if DEBUG_LOG
  DBG_PRINTLN("[EPD] Done.");
#endif

  return true;
}

void sleepUntilNextSchedule(const Config &cfg, bool hasTime, const struct tm &now) {
  if (!hasTime) {
    INK_LOG_WARN("sleep_time_fallback", "Clock unavailable; using bounded fallback sleep");
    goDeepSleepMinutes(1440);
    return;
  }

  int curMinOfDay = now.tm_hour * 60 + now.tm_min;
  int targetMin   = (int)cfg.refresh_hour * 60;
  int delta;

  if (curMinOfDay < targetMin) delta = targetMin - curMinOfDay;
  else                         delta = 24 * 60 - (curMinOfDay - targetMin);

  if (delta < 1) delta = 24 * 60;
  INK_LOG_INFO("sleep_scheduled", "Deep sleep scheduled for next refresh window");

#if DEBUG_LOG
  DBG_PRINT("[SLEEP] nowMin="); DBG_PRINT(curMinOfDay);
  DBG_PRINT(" targetMin="); DBG_PRINT(targetMin);
  DBG_PRINT(" delta="); DBG_PRINTLN(delta);
#endif

  goDeepSleepMinutes((uint32_t)delta);
}

void setup() {
  releaseAllGpioHoldsAtBoot();

  // Stabilize GPIO2 (CS_S) early to reduce strapping/board-LED interference before any other subsystems start.
  pinMode(PIN_EPD_CS_S, INPUT_PULLUP);
  pinMode(PIN_EPD_CS_M, OUTPUT);
  digitalWrite(PIN_EPD_CS_M, HIGH);

  setCpuFrequencyMhz(80);
  // 不要碰 LED_BUILTIN（很多板子 LED=GPIO2，会跟 CS_S 打架）
  // pinMode(LED_BUILTIN, OUTPUT);
  // digitalWrite(LED_BUILTIN, LOW);

  DBG_BEGIN();
  delay(200);
  INK_LOG_INFO("firmware_boot", "InkTime 13.3 firmware boot started");

#if DEBUG_LOG
  DBG_PRINTLN();
  DBG_PRINTLN("===== ESP32 InkTime Daily Photo boot (13.3E6 via epd.h) =====");
#endif

  if (isFactoryResetRequestedAtBoot()) {
#if DEBUG_LOG
    DBG_PRINTLN("[BOOT] factory reset requested at boot -> clear NVS + reset WiFi driver");
#endif
    clearConfigNVS();

    WiFi.disconnect(true, true);
    WiFi.mode(WIFI_OFF);
    esp_wifi_stop();
    delay(200);
  }

  randomSeed(esp_random());
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
    INK_LOG_WARN("wifi_degraded_mode", "Wi-Fi unavailable; entering bounded recovery path");
#if DEBUG_LOG
    DBG_PRINTLN("[BOOT] connect failed -> AP portal");
#endif
    startConfigPortal();
  }

  struct tm timeinfo;
  bool hasTime = syncTime(g_cfg, timeinfo);

  bool ok = downloadAndShowDaily(g_cfg);
  if (!ok) {
    INK_LOG_ERROR("display_pipeline_failed", "Payload download or display pipeline failed");
#if DEBUG_LOG
    DBG_PRINTLN("[BOOT] downloadAndShowDaily FAILED");
#endif
  }

  if (!hasTime) {
    struct tm tmp;
    if (syncTime(g_cfg, tmp)) sleepUntilNextSchedule(g_cfg, true, tmp);
    else                      sleepUntilNextSchedule(g_cfg, false, timeinfo);
  } else {
    sleepUntilNextSchedule(g_cfg, true, timeinfo);
  }
}

void loop() {}
