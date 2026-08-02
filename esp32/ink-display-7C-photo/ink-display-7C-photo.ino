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
#include "offline_schedule_core.h"
#include "queue_client_core.h"
#include "queue_runtime_types.h"
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
static const uint8_t AP_MAX_SAVE_ATTEMPTS = 5;

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
#define DEVICE_QUEUE_MANIFEST_PATH "/api/device/v1/queue/manifest"
#define DEVICE_QUEUE_ACK_PATH "/api/device/v1/queue/ack"
#define DEVICE_OFFLINE_SCHEDULE_PATH "/api/device/v1/offline-schedule"
#define INKTIME_FIRMWARE_VERSION "2.5.0"

// No trusted CA provisioning exists yet. HTTPS is rejected by default instead
// of silently downgrading certificate verification. Isolated LAN HTTP remains
// supported; an explicit development override prints a warning.
#ifndef INKTIME_ALLOW_INSECURE_DEVICE_HTTP
#define INKTIME_ALLOW_INSECURE_DEVICE_HTTP 0
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
  inktime::OfflineSlot schedule_slots[inktime::kMaxOfflineSlots];
  uint8_t schedule_count;
  uint16_t prefetch_lead_minutes;
  String  delivery_mode;
  String  button_wake_action;
  uint32_t config_version;
  bool    valid;
};

const char*  DEFAULT_HOSTPORT = "";
const int32_t DEFAULT_TZ_MINUTES = 8 * 60;
const uint8_t DEFAULT_HOUR    = 8;
const uint8_t DEFAULT_MINUTE  = 0;
const uint16_t DEFAULT_PREFETCH_LEAD_MINUTES = 5;

static bool parseOfflineClock(const String &value, inktime::OfflineSlot &slot) {
  if (value.length() != 5 || value[2] != ':') return false;
  if (value[0] < '0' || value[0] > '9' || value[1] < '0' || value[1] > '9'
      || value[3] < '0' || value[3] > '9' || value[4] < '0' || value[4] > '9') return false;
  slot.hour = static_cast<uint8_t>((value[0] - '0') * 10 + value[1] - '0');
  slot.minute = static_cast<uint8_t>((value[3] - '0') * 10 + value[4] - '0');
  return inktime::validOfflineSlot(slot);
}

static String offlineClock(const inktime::OfflineSlot &slot) {
  char value[6] = {0};
  snprintf(value, sizeof(value), "%02u:%02u", slot.hour, slot.minute);
  return String(value);
}

static bool validDeliveryMode(const String &value) {
  return value == "legacy_online" || value == "stock_compat"
      || value == "inktime_offline_schedule";
}

static bool validButtonWakeAction(const String &value) {
  return value == "check_new" || value == "local_next";
}

static void applyFixedTimezoneWithoutNtp(int32_t offsetMinutes) {
  const int32_t absoluteMinutes = offsetMinutes < 0 ? -offsetMinutes : offsetMinutes;
  const char sign = offsetMinutes >= 0 ? '-' : '+';  // POSIX TZ signs are reversed.
  char timezone[24] = {0};
  snprintf(
    timezone,
    sizeof(timezone),
    "UTC%c%02ld:%02ld",
    sign,
    static_cast<long>(absoluteMinutes / 60),
    static_cast<long>(absoluteMinutes % 60)
  );
  setenv("TZ", timezone, 1);
  tzset();
}

static bool applyRemoteSchedule(JsonObject remoteConfig, int schemaVersion, Config &candidate) {
  if (schemaVersion < 3) {
    inktime::OfflineSlot legacy = {};
    String schedule = remoteConfig["schedule"] | "";
    if (!parseOfflineClock(schedule, legacy)) return false;
    candidate.schedule_count = 1;
    candidate.schedule_slots[0] = legacy;
    candidate.refresh_hour = legacy.hour;
    candidate.refresh_minute = legacy.minute;
    return true;
  }
  String delivery = remoteConfig["delivery_mode"] | candidate.delivery_mode;
  String button = remoteConfig["button_wake_action"] | candidate.button_wake_action;
  const JsonVariantConst leadValue = remoteConfig["prefetch_lead_minutes"];
  if (!leadValue.isNull() && (!leadValue.is<int>() || leadValue.is<bool>())) return false;
  int lead = leadValue.isNull() ? static_cast<int>(candidate.prefetch_lead_minutes) : (leadValue | -1);
  if (!validDeliveryMode(delivery) || !validButtonWakeAction(button) || lead < 0 || lead > 120) return false;

  JsonArray rawTimes = remoteConfig["schedule_times"].as<JsonArray>();
  if (rawTimes.isNull() || rawTimes.size() == 0U || rawTimes.size() > inktime::kMaxOfflineSlots) return false;
  inktime::OfflineSlot slots[inktime::kMaxOfflineSlots] = {};
  for (size_t index = 0; index < rawTimes.size(); ++index) {
    if (!parseOfflineClock(rawTimes[index] | "", slots[index])) return false;
  }
  if (!inktime::validateOfflineSlots(slots, static_cast<uint8_t>(rawTimes.size()))) return false;
  candidate.delivery_mode = delivery;
  candidate.button_wake_action = button;
  candidate.prefetch_lead_minutes = static_cast<uint16_t>(lead);
  candidate.schedule_count = static_cast<uint8_t>(rawTimes.size());
  for (uint8_t index = 0; index < candidate.schedule_count; ++index) candidate.schedule_slots[index] = slots[index];
  candidate.refresh_hour = slots[0].hour;
  candidate.refresh_minute = slots[0].minute;
  return true;
}

static void setLegacySchedule(Config &cfg) {
  cfg.schedule_count = 1;
  cfg.schedule_slots[0] = {cfg.refresh_hour, cfg.refresh_minute};
  for (uint8_t index = 1; index < inktime::kMaxOfflineSlots; ++index) {
    cfg.schedule_slots[index] = {0, 0};
  }
}

static bool loadStoredSchedule(Config &cfg) {
  const uint8_t count = prefs.getUChar("scnt", 0);
  if (count == 0) {
    setLegacySchedule(cfg);
    return true;
  }
  if (count > inktime::kMaxOfflineSlots) return false;
  inktime::OfflineSlot slots[inktime::kMaxOfflineSlots] = {};
  for (uint8_t index = 0; index < count; ++index) {
    const String key = String("s") + String(index);
    if (!parseOfflineClock(prefs.getString(key.c_str(), ""), slots[index])) return false;
  }
  if (!inktime::validateOfflineSlots(slots, count)) return false;
  cfg.schedule_count = count;
  for (uint8_t index = 0; index < count; ++index) cfg.schedule_slots[index] = slots[index];
  for (uint8_t index = count; index < inktime::kMaxOfflineSlots; ++index) {
    cfg.schedule_slots[index] = {0, 0};
  }
  cfg.refresh_hour = slots[0].hour;
  cfg.refresh_minute = slots[0].minute;
  return true;
}

Config g_cfg;
uint8_t* frameData = nullptr;
size_t frameDataSize = 0;
bool frameIndexed4 = false;
bool frameNativePalette = false;
bool serverConfigChanged = false;
bool currentPayloadShaVerified = false;
bool currentDisplaySkipped = false;
bool currentFromQueue = false;
bool currentPrefetchOnly = false;
bool enhancedNetworkWakeRequested = false;
bool currentPayloadIntegrityTrusted = false;
String portalSetupSecret;
String portalNonce;
uint8_t portalSaveAttempts = 0;
bool portalSaveAllowed = false;
String currentReleaseId;
String currentRenderProfile;
String currentPayloadSha256;
String currentQueueItemId;
int64_t currentQueueVersion = -1;
String lastDeviceErrorCode;
String lastDeviceErrorMessage;
uint32_t lastRefreshDurationMs = 0;

static int calculateSha256(const unsigned char* input, size_t length, unsigned char output[32]) {
#if MBEDTLS_VERSION_MAJOR >= 3
  return mbedtls_sha256(input, length, output, 0);
#else
  return mbedtls_sha256_ret(input, length, output, 0);
#endif
}

static bool backendTransportAllowed(const String &base) {
  if (base.startsWith("https://")) return true;
#if INKTIME_ALLOW_INSECURE_DEVICE_HTTP
  return true;
#else
  lastDeviceErrorCode = "DEVICE-HTTP-DISALLOWED";
  lastDeviceErrorMessage = "正式裝置 Backend 必須使用 HTTPS";
  return false;
#endif
}

static void configureHttpClient(HTTPClient &client, uint32_t timeoutMs) {
  client.setConnectTimeout(10000);
  client.setTimeout(timeoutMs);
  client.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
}

static String randomPortalSecret() {
  char value[25];
  for (uint8_t i = 0; i < 12; ++i) snprintf(value + i * 2, 3, "%02x", static_cast<unsigned>(esp_random() & 0xff));
  return String(value);
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

static StoredDisplayRecord loadDisplayRecord() {
  prefs.begin("dashcfg", true);
  const uint8_t version = prefs.getUChar("disp_ver", 0);
  StoredDisplayRecord record = {
    prefs.getString("last_sha", ""),
    prefs.getString("last_rel", ""),
    prefs.getString("last_prof", ""),
    prefs.getString("last_board", ""),
    static_cast<int16_t>(prefs.getShort("last_rot", -1)),
    prefs.getBool("last_ok", false),
    false,
  };
  prefs.end();
  record.valid = version == 1U
    && inktime::isSha256HexValue(record.sha256.c_str())
    && record.releaseId.length() > 0U && record.releaseId.length() <= 128U
    && record.renderProfile.length() > 0U && record.renderProfile.length() <= 64U
    && record.boardProfile.length() > 0U && record.boardProfile.length() <= 96U
    && (record.rotation == 0 || record.rotation == 180);
  return record;
}

static void saveDisplayRecord(const Config &cfg, bool succeeded) {
  if (!inktime::isSha256HexValue(currentPayloadSha256.c_str())
      || currentReleaseId.length() == 0U || currentReleaseId.length() > 128U
      || currentRenderProfile.length() == 0U || currentRenderProfile.length() > 64U) {
    return;
  }
  prefs.begin("dashcfg", false);
  prefs.putUChar("disp_ver", 1U);
  prefs.putString("last_sha", currentPayloadSha256);
  prefs.putString("last_rel", currentReleaseId);
  prefs.putString("last_prof", currentRenderProfile);
  prefs.putString("last_board", kBoardConfig.name);
  prefs.putShort("last_rot", cfg.rotate180 ? 180 : 0);
  prefs.putBool("last_ok", succeeded);
  prefs.end();
}

static bool shouldSkipCurrentDisplay(const Config &cfg) {
  const StoredDisplayRecord stored = loadDisplayRecord();
#if INKTIME_PHOTOPAINTER_ENABLED
  const bool forcedRefresh = photoPainter.forceNetworkRefresh();
#else
  const bool forcedRefresh = false;
#endif
  const inktime::DisplayRecord record = {
    stored.sha256.c_str(),
    stored.releaseId.c_str(),
    stored.renderProfile.c_str(),
    stored.boardProfile.c_str(),
    stored.rotation,
    stored.succeeded,
    stored.valid,
  };
  const inktime::DisplayCandidate candidate = {
    currentPayloadSha256.c_str(),
    currentReleaseId.c_str(),
    currentRenderProfile.c_str(),
    kBoardConfig.name,
    static_cast<int16_t>(cfg.rotate180 ? 180 : 0),
    currentPayloadShaVerified,
    currentPayloadIntegrityTrusted,
    forcedRefresh,
    false,
    false,
  };
  return inktime::shouldSkipDisplay(record, candidate);
}

static bool validPendingQueueAck(PendingQueueAck &pending) {
  const uint8_t event = static_cast<uint8_t>(pending.event);
  pending.valid = inktime::boundedText(
      pending.queueItemId.c_str(), inktime::kQueueIdentifierMaxBytes)
    && pending.queueVersion >= 0
    && event <= static_cast<uint8_t>(inktime::QueueEvent::DisplayFailed)
    && pending.errorCode.length() <= 64U
    && (!pending.delayedTerminal || (
        (pending.event == inktime::QueueEvent::DisplayCompleted
          || pending.event == inktime::QueueEvent::DisplayFailed)
        && inktime::boundedText(pending.releaseId.c_str(), inktime::kQueueIdentifierMaxBytes)));
  return pending.valid;
}

static String ackJournalKey(char prefix, uint8_t index) {
  return String(prefix) + String(index);
}

static PendingQueueAck readAckJournalEntry(Preferences &journal, uint8_t index) {
  PendingQueueAck pending = {
    journal.getString(ackJournalKey('i', index).c_str(), ""),
    journal.getInt(ackJournalKey('v', index).c_str(), -1),
    static_cast<inktime::QueueEvent>(
      journal.getUChar(ackJournalKey('e', index).c_str(), 255U)),
    journal.getBool(ackJournalKey('s', index).c_str(), false),
    journal.getString(ackJournalKey('r', index).c_str(), ""),
    journal.getBool(ackJournalKey('d', index).c_str(), false),
    journal.getString(ackJournalKey('l', index).c_str(), ""),
    false,
  };
  validPendingQueueAck(pending);
  return pending;
}

static void writeAckJournalEntry(
  Preferences &journal,
  uint8_t index,
  const PendingQueueAck &pending
) {
  journal.putString(ackJournalKey('i', index).c_str(), pending.queueItemId);
  journal.putInt(ackJournalKey('v', index).c_str(), pending.queueVersion);
  journal.putUChar(
    ackJournalKey('e', index).c_str(), static_cast<uint8_t>(pending.event));
  journal.putBool(ackJournalKey('s', index).c_str(), pending.displaySkipped);
  journal.putString(ackJournalKey('r', index).c_str(), pending.errorCode);
  journal.putBool(ackJournalKey('d', index).c_str(), pending.delayedTerminal);
  journal.putString(ackJournalKey('l', index).c_str(), pending.releaseId);
}

static bool samePendingQueueAck(
  const PendingQueueAck &left,
  const PendingQueueAck &right
) {
  return left.queueItemId == right.queueItemId
    && left.queueVersion == right.queueVersion
    && left.event == right.event;
}

static uint8_t ackJournalCount(Preferences &journal) {
  return min(
    journal.getUChar("count", 0U),
    static_cast<uint8_t>(inktime::kMaxAckJournalEntries));
}

static void removeLegacyPendingQueueAck() {
  prefs.begin("dashcfg", false);
  prefs.remove("ack_item");
  prefs.remove("ack_ver");
  prefs.remove("ack_event");
  prefs.remove("ack_skip");
  prefs.remove("ack_error");
  prefs.end();
}

static void persistPendingQueueAck(const PendingQueueAck &pending) {
  if (!pending.valid) return;
  Preferences journal;
  journal.begin("acklog", false);
  uint8_t count = ackJournalCount(journal);
  for (uint8_t index = 0; index < count; ++index) {
    const PendingQueueAck existing = readAckJournalEntry(journal, index);
    if (existing.valid && samePendingQueueAck(existing, pending)) {
      journal.end();
      return;
    }
  }
  uint8_t insertAt = count;
  if (count >= inktime::kMaxAckJournalEntries) {
    for (uint8_t index = 1; index < inktime::kMaxAckJournalEntries; ++index) {
      const PendingQueueAck shifted = readAckJournalEntry(journal, index);
      if (shifted.valid) writeAckJournalEntry(journal, index - 1U, shifted);
    }
    insertAt = inktime::kMaxAckJournalEntries - 1U;
  } else {
    ++count;
  }
  writeAckJournalEntry(journal, insertAt, pending);
  journal.putUChar("count", count);
  journal.end();
}

static void removePendingQueueAck(const PendingQueueAck &pending) {
  Preferences journal;
  journal.begin("acklog", false);
  const uint8_t count = ackJournalCount(journal);
  uint8_t found = count;
  for (uint8_t index = 0; index < count; ++index) {
    const PendingQueueAck existing = readAckJournalEntry(journal, index);
    if (existing.valid && samePendingQueueAck(existing, pending)) {
      found = index;
      break;
    }
  }
  if (found < count) {
    for (uint8_t index = found + 1U; index < count; ++index) {
      const PendingQueueAck shifted = readAckJournalEntry(journal, index);
      if (shifted.valid) writeAckJournalEntry(journal, index - 1U, shifted);
    }
    const uint8_t last = count - 1U;
    journal.remove(ackJournalKey('i', last).c_str());
    journal.remove(ackJournalKey('v', last).c_str());
    journal.remove(ackJournalKey('e', last).c_str());
    journal.remove(ackJournalKey('s', last).c_str());
    journal.remove(ackJournalKey('r', last).c_str());
    journal.remove(ackJournalKey('d', last).c_str());
    journal.remove(ackJournalKey('l', last).c_str());
    journal.putUChar("count", last);
  }
  journal.end();
}

static PendingQueueAck loadPendingQueueAck() {
  Preferences journal;
  journal.begin("acklog", true);
  const uint8_t count = ackJournalCount(journal);
  for (uint8_t index = 0; index < count; ++index) {
    PendingQueueAck pending = readAckJournalEntry(journal, index);
    if (pending.valid) {
      journal.end();
      return pending;
    }
  }
  journal.end();

  // Migrate the pre-journal single pending record without losing an event
  // after an upgrade from the previous firmware.
  prefs.begin("dashcfg", true);
  PendingQueueAck legacy = {
    prefs.getString("ack_item", ""),
    prefs.getInt("ack_ver", -1),
    static_cast<inktime::QueueEvent>(prefs.getUChar("ack_event", 255U)),
    prefs.getBool("ack_skip", false),
    prefs.getString("ack_error", ""),
    false,
    "",
    false,
  };
  prefs.end();
  if (validPendingQueueAck(legacy)) {
    persistPendingQueueAck(legacy);
    removeLegacyPendingQueueAck();
    return legacy;
  }
  return legacy;
}

static void clearPendingQueueAck() {
  const PendingQueueAck pending = loadPendingQueueAck();
  if (pending.valid) removePendingQueueAck(pending);
}

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
  cfg.prefetch_lead_minutes = prefs.getUShort("prefetch", DEFAULT_PREFETCH_LEAD_MINUTES);
  cfg.delivery_mode     = prefs.getString("delivery", "legacy_online");
  cfg.button_wake_action = prefs.getString("button", "check_new");
  cfg.config_version   = prefs.getULong("cfgver", 0);
  if (!validDeliveryMode(cfg.delivery_mode)) cfg.delivery_mode = "legacy_online";
  if (!validButtonWakeAction(cfg.button_wake_action)) cfg.button_wake_action = "check_new";
  if (cfg.prefetch_lead_minutes > 120U) cfg.prefetch_lead_minutes = DEFAULT_PREFETCH_LEAD_MINUTES;
  if (!loadStoredSchedule(cfg)) setLegacySchedule(cfg);
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
  prefs.putUShort("prefetch", cfg.prefetch_lead_minutes);
  prefs.putString("delivery", cfg.delivery_mode);
  prefs.putString("button", cfg.button_wake_action);
  prefs.putUChar("scnt", cfg.schedule_count);
  for (uint8_t index = 0; index < cfg.schedule_count && index < inktime::kMaxOfflineSlots; ++index) {
    const String key = String("s") + String(index);
    prefs.putString(key.c_str(), offlineClock(cfg.schedule_slots[index]));
  }
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
  WiFi.disconnect(false, false);
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
  html += F("<form method='POST' action='/save'><input type='hidden' name='setup_secret' value='"); html += portalSetupSecret; html += F("'><input type='hidden' name='nonce' value='"); html += portalNonce; html += F("'>");

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

#if INKTIME_ALLOW_INSECURE_DEVICE_HTTP
  html += F("<p style='color:#a33'><strong>LAN build：</strong>HTTP 僅限可信任 LAN／IoT VLAN，沒有 TLS 保護；可用 http://host:port。</p>");
  html += F("InkTime 伺服器 (https:// 或可信任 LAN http://host:port):<br><input name='hostport' size='40' value='");
#else
  html += F("<p><strong>Secure build：</strong>InkTime 伺服器只允許 HTTPS。</p>");
  html += F("InkTime 伺服器 (https://host:port):<br><input name='hostport' size='40' value='");
#endif
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
  if (!portalSaveAllowed || portalSaveAttempts++ >= AP_MAX_SAVE_ATTEMPTS ||
      server.arg("setup_secret") != portalSetupSecret || server.arg("nonce") != portalNonce) {
    server.send(403, "text/plain; charset=utf-8", "PAIRING-001 配對授權失效");
    if (portalSaveAttempts >= AP_MAX_SAVE_ATTEMPTS) {
      portalSaveAllowed = false;
      server.stop();
      goDeepSleepMinutes(minutesToNextRefreshFromLastEpoch(g_cfg));
    }
    return;
  }
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
  const int schemeEnd = host.indexOf("://");
  const String hostOrigin = schemeEnd >= 0 ? host.substring(schemeEnd + 3) : String("");
  const bool unsafeOrigin = hostOrigin.length() == 0 || hostOrigin.indexOf('/') >= 0
    || hostOrigin.indexOf('?') >= 0 || hostOrigin.indexOf('#') >= 0;
#if INKTIME_ALLOW_INSECURE_DEVICE_HTTP
  const bool allowedScheme = host.startsWith("https://") || host.startsWith("http://");
#else
  const bool allowedScheme = host.startsWith("https://");
#endif
  if (ssid.length() > 32 || pass.length() > 63 || host.length() > 240 || deviceToken.length() > 256
      || host.indexOf('@') >= 0 || !allowedScheme || unsafeOrigin) {
    server.send(400, "text/plain; charset=utf-8", "PAIRING-002 設定格式或長度不合法");
    return;
  }

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
  portalSaveAllowed = false; portalSetupSecret = ""; portalNonce = "";

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
static void goDeepSleepSeconds(uint64_t seconds) {
  if (seconds < 1U) seconds = 1U;

  uint64_t us = seconds * 1000000ULL;

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

  WiFi.disconnect(false, false);
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

void goDeepSleepMinutes(uint32_t minutes) {
  if (minutes < 1U) minutes = 1U;
  if (minutes > 1440U) minutes = 1440U;
  goDeepSleepSeconds(static_cast<uint64_t>(minutes) * 60ULL);
}

void goDeepSleepUntilEpoch(time_t nowEpoch, time_t nextEpoch) {
  goDeepSleepSeconds(inktime::exactSleepSeconds(
    static_cast<uint64_t>(nowEpoch),
    static_cast<uint64_t>(nextEpoch)
  ));
}

// =======================
//  启动 AP 配置模式
// =======================
void startConfigPortal() {
#if DEBUG_LOG
  DBG_PRINTLN("[CFG] enter startConfigPortal()");
#endif

  wifiHardResetForPortal();

  portalSetupSecret = randomPortalSecret();
  portalNonce = randomPortalSecret();
  portalSaveAttempts = 0; portalSaveAllowed = true;
  String chipHex = String((uint32_t)ESP.getEfuseMac(), HEX);
  chipHex.toUpperCase();
  while (chipHex.length() < 8) chipHex = "0" + chipHex;
  String shortId = chipHex.substring(chipHex.length() - 6);
  String apSsid = "InkTime-" + shortId;
  String apPassword = randomPortalSecret(); // never derived from SSID, MAC, or chip ID

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
  // USB power alone is not authorization to alter Wi-Fi or a device token.
  if (!isFactoryResetRequestedAtBoot()) return false;
  portalSetupSecret = randomPortalSecret();
  portalNonce = randomPortalSecret();
  portalSaveAttempts = 0;
  portalSaveAllowed = true;

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
bool connectWiFi(const Config &cfg, uint32_t timeout_ms = 12000) {
#if DEBUG_LOG
  DBG_PRINTLN("[WIFI] connectWiFi()");
  DBG_PRINT("[WIFI] target ssid="); DBG_PRINTLN(cfg.wifi_ssid);
#endif

  if (cfg.wifi_ssid.isEmpty()) return false;

  WiFi.disconnect(false, false);
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

static bool normalizedBackendBase(const Config &cfg, String &base) {
  base = cfg.backend_hostport;
  base.trim();
  if (!base.startsWith("http://") && !base.startsWith("https://")) base = "https://" + base;
  while (base.endsWith("/")) base.remove(base.length() - 1U);
  const int schemeEnd = base.indexOf("://");
  const String origin = schemeEnd >= 0 ? base.substring(schemeEnd + 3) : String("");
  if (origin.length() == 0U || origin.indexOf('/') >= 0 || origin.indexOf('?') >= 0
      || origin.indexOf('#') >= 0 || origin.indexOf('@') >= 0) {
    lastDeviceErrorCode = "DEVICE-BACKEND-ORIGIN";
    lastDeviceErrorMessage = "Backend 必須是不含帳密、路徑、Query 或 Fragment 的 Origin";
    return false;
  }
  return backendTransportAllowed(base);
}

static bool queueAckIdempotencyKey(const PendingQueueAck &pending, String &key) {
  char material[320];
  if (!inktime::idempotencyMaterial(
        pending.queueItemId.c_str(), pending.queueVersion, pending.event, material, sizeof(material))) {
    return false;
  }
  unsigned char digest[32];
  if (calculateSha256(
        reinterpret_cast<const unsigned char*>(material), strlen(material), digest) != 0) {
    return false;
  }
  char output[65];
  for (size_t index = 0; index < 32U; ++index) {
    snprintf(output + index * 2U, 3U, "%02x", digest[index]);
  }
  output[64] = '\0';
  key = output;
  return true;
}

static bool sendQueueAck(const Config &cfg, const PendingQueueAck &pending, bool persistFirst) {
  if (!pending.valid) return false;
  if (persistFirst) persistPendingQueueAck(pending);
  String base;
  if (WiFi.status() != WL_CONNECTED || !normalizedBackendBase(cfg, base)) return false;
  String idempotencyKey;
  if (!queueAckIdempotencyKey(pending, idempotencyKey)) {
    lastDeviceErrorCode = "DEVICE-QUEUE-ACK-KEY";
    lastDeviceErrorMessage = "Queue ACK idempotency key 無法建立";
    return false;
  }
  JsonDocument payload;
  payload["queue_item_id"] = pending.queueItemId;
  payload["queue_version"] = pending.queueVersion;
  payload["event"] = inktime::queueEventName(pending.event);
  payload["idempotency_key"] = idempotencyKey;
  if (pending.displaySkipped) {
    payload["display_skipped"] = true;
    payload["skip_reason"] = "same_sha256";
  }
  if (pending.delayedTerminal) {
    payload["ack_mode"] = "delayed_terminal";
    payload["release_id"] = pending.releaseId;
  }
  if (pending.errorCode.length() > 0U) payload["error_code"] = pending.errorCode;
  String body;
  serializeJson(payload, body);

  for (uint8_t attempt = 0; attempt <= inktime::kQueueRetryLimit; ++attempt) {
    HTTPClient ackHttp;
    configureHttpClient(ackHttp, 15000);
    if (!ackHttp.begin(base + String(DEVICE_QUEUE_ACK_PATH))) break;
    ackHttp.addHeader("Authorization", "Bearer " + cfg.device_token);
    ackHttp.addHeader("Content-Type", "application/json");
    const int status = ackHttp.POST(body);
    ackHttp.end();
    const inktime::AckDecision decision = inktime::ackDecision(status, attempt);
    if (decision == inktime::AckDecision::Accepted) {
      removePendingQueueAck(pending);
      return true;
    }
    if (decision == inktime::AckDecision::StaleManifest) {
      removePendingQueueAck(pending);
      lastDeviceErrorCode = "DEVICE-QUEUE-STALE";
      lastDeviceErrorMessage = "QUEUE-003 Queue version 已過期；下次重新取得 Manifest";
      return false;
    }
    if (decision == inktime::AckDecision::AuthorizationFailed) {
      lastDeviceErrorCode = "DEVICE-QUEUE-AUTH";
      lastDeviceErrorMessage = "Queue ACK Token／authorization 被拒絕";
      return false;
    }
    if (decision != inktime::AckDecision::Retry) break;
    delay(250U * (attempt + 1U));
  }
  lastDeviceErrorCode = "DEVICE-QUEUE-ACK-RETRY";
  lastDeviceErrorMessage = "Queue ACK 已達有界 retry 上限；pending event 已保留";
  return false;
}

static bool sendQueueEvent(
  const Config &cfg,
  inktime::QueueEvent event,
  bool displaySkipped = false,
  const String &errorCode = String(""),
  bool delayedTerminal = false
) {
  PendingQueueAck pending = {
    currentQueueItemId,
    static_cast<int32_t>(currentQueueVersion),
    event,
    displaySkipped,
    errorCode.substring(0, 64),
    delayedTerminal,
    currentReleaseId,
    currentQueueItemId.length() > 0U && currentQueueVersion >= 0,
  };
  return sendQueueAck(cfg, pending, true);
}

static bool resumePendingQueueAck(const Config &cfg) {
  for (uint8_t attempt = 0; attempt < inktime::kMaxAckJournalEntries; ++attempt) {
    const PendingQueueAck pending = loadPendingQueueAck();
    if (!pending.valid) return true;
    if (!sendQueueAck(cfg, pending, false)) return false;
  }
  return false;
}

// =======================
//  下载每日相册 BIN
// =======================
bool downloadLatestPhotoBin(Config &cfg) {
  lastDeviceErrorCode = "";
  lastDeviceErrorMessage = "";
  currentFromQueue = false;
  currentQueueItemId = "";
  currentQueueVersion = -1;
  currentDisplaySkipped = false;
  currentPayloadShaVerified = false;
  currentPayloadIntegrityTrusted = false;
  currentPayloadSha256 = "";
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

  String base;
  if (!normalizedBackendBase(cfg, base)) return false;
  String manifestUrl = base + String(DEVICE_MANIFEST_PATH);

#if DEBUG_LOG
  DBG_PRINTLN("[HTTP] 取得版本 Manifest（Authorization 已遮蔽）");
#endif

  HTTPClient manifestHttp;
  configureHttpClient(manifestHttp, 30000);
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
  if (jsonError || (schemaVersion != 1 && schemaVersion != 2 && schemaVersion != 3)
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
          || remoteConfig["schema_version"].as<int>() == 2
          || remoteConfig["schema_version"].as<int>() == 3)) {
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
    Config candidate = cfg;
    bool validSchedule = applyRemoteSchedule(
      remoteConfig,
      remoteConfig["schema_version"] | 0,
      candidate
    );
    bool validRemote = offsetMinutes >= -12 * 60 && offsetMinutes <= 14 * 60
      && remoteHour >= 0 && remoteHour <= 23 && remoteMinute >= 0 && remoteMinute <= 59
      && (rotation == 0 || rotation == 180) && compatiblePanel
      && desiredConfigVersion >= cfg.config_version && validSchedule;
    if (!validRemote) {
      lastDeviceErrorCode = "DEVICE-CONFIG-PROFILE";
      lastDeviceErrorMessage = "遠端設定版本或面板 Profile 與韌體不相容";
      return false;
    }
    if (
        cfg.tz_offset_minutes != offsetMinutes || cfg.refresh_hour != remoteHour
        || cfg.refresh_minute != remoteMinute || cfg.rotate180 != (rotation == 180)
        || cfg.config_version != desiredConfigVersion
        || cfg.delivery_mode != candidate.delivery_mode
        || cfg.schedule_count != candidate.schedule_count
        || cfg.prefetch_lead_minutes != candidate.prefetch_lead_minutes
        || cfg.button_wake_action != candidate.button_wake_action) {
      candidate.tz_offset_minutes = offsetMinutes;
      candidate.rotate180 = rotation == 180;
      candidate.config_version = desiredConfigVersion;
      cfg = candidate;
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
    if (photoPainter.loadCachedFrame(sourceHash, rotation, &cachedFrame, expectedSha.c_str())) {
      heap_caps_free(packed);
      if (frameData) heap_caps_free(frameData);
      frameData = cachedFrame;
      frameDataSize = inktime::kPhotoPainterFrameBytes;
      frameIndexed4 = true;
      frameNativePalette = true;
      currentReleaseId = manifest["release_id"] | "";
      currentRenderProfile = renderProfile;
      currentPayloadShaVerified = true;
      currentPayloadIntegrityTrusted = true;
      currentPayloadSha256 = expectedSha;
      saveLastPhotoIndex(fileIndex);
      return true;
    }
#endif

    String fileUrl = base + String(downloadBaseRaw) + fileName;
    HTTPClient fileHttp;
    configureHttpClient(fileHttp, 60000);
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
    currentPayloadShaVerified = true;
    currentPayloadIntegrityTrusted = true;
    currentPayloadSha256 = expectedSha;

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
          &nativeFrame,
          expectedSha.c_str())) {
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

#if INKTIME_PHOTOPAINTER_ENABLED
static bool downloadOfflineScheduleSlot(
  const Config &cfg,
  const String &base,
  const String &downloadUrl,
  const String &expectedSha,
  const String &pixelFormat,
  int64_t expectedSize
) {
  if (!inktime::isSha256Hex(expectedSha.c_str())
      || !inktime::isSafeQueueDownloadPath(downloadUrl.c_str(), downloadUrl.length())) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-ITEM";
    lastDeviceErrorMessage = "離線排程 Slot 身分或下載路徑不合法";
    return false;
  }
  const bool indexed4 = pixelFormat == "indexed4";
  if (pixelFormat != "indexed4" && pixelFormat != "2bpp") {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-ITEM";
    lastDeviceErrorMessage = "離線排程 Slot pixel_format 不相容";
    return false;
  }
  const size_t packedSize = static_cast<size_t>(FB_WIDTH) * FB_HEIGHT
    / (indexed4 ? 2U : 4U);
  if (expectedSize != static_cast<int64_t>(packedSize)) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-SIZE";
    lastDeviceErrorMessage = "離線排程 Slot size 與 pixel format 不一致";
    return false;
  }
  uint8_t* packed = photoPainter.allocateWireBuffer(packedSize);
  if (packed == nullptr) {
    lastDeviceErrorCode = "DEVICE-MEMORY";
    lastDeviceErrorMessage = "無法配置離線排程 Slot 緩衝區";
    return false;
  }
  HTTPClient fileHttp;
  configureHttpClient(fileHttp, 60000);
  const char* fileHeaders[] = {"Content-Type"};
  fileHttp.collectHeaders(fileHeaders, 1);
  if (!fileHttp.begin(base + downloadUrl)) {
    heap_caps_free(packed);
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-URL";
    lastDeviceErrorMessage = "離線排程 Slot URL 無法初始化";
    return false;
  }
  fileHttp.addHeader("Authorization", "Bearer " + cfg.device_token);
  const int fileStatus = fileHttp.GET();
  const String fileContentType = fileHttp.header("Content-Type");
  if (fileStatus != HTTP_CODE_OK || fileHttp.getSize() != static_cast<int>(packedSize)
      || !fileContentType.startsWith("application/octet-stream")) {
    fileHttp.end();
    heap_caps_free(packed);
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-DOWNLOAD";
    lastDeviceErrorMessage = "離線排程 Slot HTTP／Content-Type／長度不合法";
    return false;
  }
  WiFiClient* stream = fileHttp.getStreamPtr();
  size_t total = 0U;
  const uint32_t started = millis();
  while (total < packedSize && millis() - started < 60000U) {
    const size_t available = stream->available();
    if (available == 0U) {
      if (!fileHttp.connected()) break;
      delay(1);
      continue;
    }
    const size_t count = min(available, packedSize - total);
    const int received = stream->read(packed + total, count);
    if (received > 0) total += static_cast<size_t>(received);
  }
  fileHttp.end();
  if (total != packedSize) {
    heap_caps_free(packed);
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-DOWNLOAD";
    lastDeviceErrorMessage = "離線排程 Slot Payload 未完整下載";
    return false;
  }
  unsigned char digest[32];
  if (calculateSha256(packed, packedSize, digest) != 0) {
    heap_caps_free(packed);
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-HASH";
    lastDeviceErrorMessage = "離線排程 Slot SHA-256 計算失敗";
    return false;
  }
  char actualSha[65];
  for (size_t index = 0; index < 32U; ++index) {
    snprintf(actualSha + index * 2U, 3U, "%02x", digest[index]);
  }
  actualSha[64] = '\0';
  if (!expectedSha.equalsIgnoreCase(String(actualSha))) {
    heap_caps_free(packed);
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-HASH";
    lastDeviceErrorMessage = "離線排程 Slot SHA-256 不一致";
    return false;
  }
  const inktime::DisplayRotation rotation = cfg.rotate180
    ? inktime::DisplayRotation::Rotate180
    : inktime::DisplayRotation::Rotate0;
  uint8_t* nativeFrame = nullptr;
  const uint32_t sourceHash = inktime::sourceHash32(expectedSha.c_str());
  const bool converted = photoPainter.convertAndCache(
    packed,
    packedSize,
    indexed4,
    sourceHash,
    rotation,
    &nativeFrame,
    expectedSha.c_str());
  heap_caps_free(packed);
  if (!converted || nativeFrame == nullptr) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-FRAME";
    lastDeviceErrorMessage = "離線排程正式 Frame 轉換失敗";
    return false;
  }
  const bool written = photoPainter.writeFormalFrame(
    expectedSha.c_str(), rotation, nativeFrame, inktime::kPhotoPainterFrameBytes);
  heap_caps_free(nativeFrame);
  if (!written) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-FRAME";
    lastDeviceErrorMessage = "離線排程正式 Frame 無法原子寫入 SD";
    return false;
  }
  return true;
}

static bool downloadOfflineScheduleAndFrames(Config &cfg) {
  String base;
  if (cfg.backend_hostport.length() == 0 || cfg.device_token.length() == 0
      || !normalizedBackendBase(cfg, base)) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-CONFIG";
    lastDeviceErrorMessage = "離線排程缺少 Backend 或裝置 Token";
    return false;
  }
  HTTPClient scheduleHttp;
  configureHttpClient(scheduleHttp, 30000);
  const char* scheduleHeaders[] = {"Content-Type"};
  scheduleHttp.collectHeaders(scheduleHeaders, 1);
  if (!scheduleHttp.begin(base + String(DEVICE_OFFLINE_SCHEDULE_PATH))) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-URL";
    lastDeviceErrorMessage = "離線排程 URL 無法初始化";
    return false;
  }
  scheduleHttp.addHeader("Authorization", "Bearer " + cfg.device_token);
  const int status = scheduleHttp.GET();
  const int length = scheduleHttp.getSize();
  const String contentType = scheduleHttp.header("Content-Type");
  if (status != HTTP_CODE_OK || length <= 0 || length > 32768
      || !contentType.startsWith("application/json")) {
    scheduleHttp.end();
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-HTTP";
    lastDeviceErrorMessage = "離線排程 HTTP／Content-Type／長度不合法";
    return false;
  }
  JsonDocument schedule;
  const DeserializationError jsonError = deserializeJson(schedule, scheduleHttp.getStream());
  scheduleHttp.end();
  const JsonVariantConst schema = schedule["schema_version"];
  const JsonVariantConst rawSlots = schedule["slots"];
  const String deliveryMode = schedule["delivery_mode"] | "";
  if (jsonError || schedule.overflowed() || !schema.is<int32_t>() || schema.is<bool>()
      || schema.as<int32_t>() != 1 || deliveryMode != "inktime_offline_schedule"
      || !rawSlots.is<JsonArrayConst>()) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-SCHEMA";
    lastDeviceErrorMessage = "離線排程 schema 或 delivery_mode 不相容";
    return false;
  }
  const JsonArrayConst slots = rawSlots.as<JsonArrayConst>();
  if (slots.size() == 0U || slots.size() > inktime::kMaxOfflineSlots) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-COUNT";
    lastDeviceErrorMessage = "離線排程 Slot 數量超過裝置上限";
    return false;
  }
  Config scheduleCandidate = cfg;
  JsonArrayConst rawTimes = schedule["schedule_times"].as<JsonArrayConst>();
  if (rawTimes.isNull()) rawTimes = schedule["schedule"].as<JsonArrayConst>();
  if (rawTimes.isNull() || rawTimes.size() == 0U
      || rawTimes.size() > inktime::kMaxOfflineSlots) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-SCHEMA";
    lastDeviceErrorMessage = "離線排程 schedule_times 不合法";
    return false;
  }
  inktime::OfflineSlot scheduleSlots[inktime::kMaxOfflineSlots] = {};
  for (size_t index = 0; index < rawTimes.size(); ++index) {
    if (!parseOfflineClock(rawTimes[index] | "", scheduleSlots[index])) {
      lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-SCHEMA";
      lastDeviceErrorMessage = "離線排程時刻格式不合法";
      return false;
    }
  }
  if (!inktime::validateOfflineSlots(scheduleSlots, static_cast<uint8_t>(rawTimes.size()))) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-SCHEMA";
    lastDeviceErrorMessage = "離線排程時刻未排序或重複";
    return false;
  }
  scheduleCandidate.schedule_count = static_cast<uint8_t>(rawTimes.size());
  for (uint8_t index = 0; index < scheduleCandidate.schedule_count; ++index) {
    scheduleCandidate.schedule_slots[index] = scheduleSlots[index];
  }
  scheduleCandidate.refresh_hour = scheduleSlots[0].hour;
  scheduleCandidate.refresh_minute = scheduleSlots[0].minute;
  scheduleCandidate.config_version = schedule["config_version"] | cfg.config_version;
  const int remoteLead = schedule["prefetch_lead_minutes"] | cfg.prefetch_lead_minutes;
  if (remoteLead < 0 || remoteLead > 120) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-SCHEMA";
    lastDeviceErrorMessage = "離線排程 prefetch_lead_minutes 不合法";
    return false;
  }
  scheduleCandidate.prefetch_lead_minutes = static_cast<uint16_t>(remoteLead);
  for (size_t index = 0; index < slots.size(); ++index) {
    const JsonVariantConst rawSlot = slots[index];
    if (!rawSlot.is<JsonObjectConst>()) {
      lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-ITEM";
      lastDeviceErrorMessage = "離線排程 Slot 必須是 JSON object";
      return false;
    }
    const JsonObjectConst slot = rawSlot.as<JsonObjectConst>();
    const JsonVariantConst rawSize = slot["size"];
    const JsonVariantConst rawWidth = slot["width"];
    const JsonVariantConst rawHeight = slot["height"];
    const JsonVariantConst rawSlotIndex = slot["slot_index"];
    const JsonVariantConst rawQueueVersion = slot["queue_version"];
    const String itemId = slot["queue_item_id"] | "";
    const String releaseId = slot["release_id"] | "";
    const String sha = slot["sha256"] | "";
    const String downloadUrl = slot["download_url"] | "";
    const String pixelFormat = slot["pixel_format"] | "";
    const String renderProfile = slot["render_profile"] | "";
    if (!rawSize.is<int64_t>() || rawSize.is<bool>()
        || !rawWidth.is<int32_t>() || rawWidth.is<bool>()
        || !rawHeight.is<int32_t>() || rawHeight.is<bool>()
        || !rawSlotIndex.is<int32_t>() || rawSlotIndex.is<bool>()
        || rawSlotIndex.as<int32_t>() != static_cast<int32_t>(index)
        || !rawQueueVersion.is<int32_t>() || rawQueueVersion.is<bool>()
        || rawQueueVersion.as<int32_t>() < 0
        || rawWidth.as<int32_t>() != FB_WIDTH || rawHeight.as<int32_t>() != FB_HEIGHT
        || !inktime::isSha256Hex(sha.c_str())
        || !inktime::boundedText(itemId.c_str(), inktime::kQueueIdentifierMaxBytes)
        || !inktime::boundedText(releaseId.c_str(), inktime::kQueueIdentifierMaxBytes)
        || !inktime::isSafeQueueDownloadPathForItem(
             downloadUrl.c_str(), downloadUrl.length(), itemId.c_str())
        || (renderProfile != "safe_4c" && renderProfile != String(INKTIME_PANEL_PROFILE))) {
      lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-ITEM";
      lastDeviceErrorMessage = "離線排程 Slot 身分、尺寸、SHA 或 Profile 不合法";
      return false;
    }
    currentFromQueue = true;
    currentQueueItemId = itemId;
    currentQueueVersion = rawQueueVersion.as<int32_t>();
    currentReleaseId = releaseId;
    currentRenderProfile = renderProfile;
    currentPayloadSha256 = sha;
    currentPayloadShaVerified = false;
    currentPayloadIntegrityTrusted = false;
    if (!sendQueueEvent(cfg, inktime::QueueEvent::ManifestReceived)
        || !sendQueueEvent(cfg, inktime::QueueEvent::DownloadStarted)
        || !downloadOfflineScheduleSlot(
             cfg, base, downloadUrl, sha, pixelFormat, rawSize.as<int64_t>())
        || !sendQueueEvent(cfg, inktime::QueueEvent::DownloadCompleted)) {
      return false;
    }
    currentPayloadShaVerified = true;
    if (!sendQueueEvent(cfg, inktime::QueueEvent::HashVerified)) return false;
  }
  String activeSchedule;
  serializeJson(schedule, activeSchedule);
  if (!photoPainter.writeActiveSchedule(activeSchedule.c_str(), activeSchedule.length())) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-STAGE";
    lastDeviceErrorMessage = "離線排程 active schedule 無法原子切換";
    return false;
  }
  cfg = scheduleCandidate;
  saveConfig(cfg);
  serverConfigChanged = true;
  currentFromQueue = false;
  currentPayloadIntegrityTrusted = true;
  return true;
}
#endif

static QueueDownloadResult downloadQueuePhotoBin(Config &cfg) {
  String base;
  if (!normalizedBackendBase(cfg, base)) return QueueDownloadResult::Failed;

  HTTPClient manifestHttp;
  configureHttpClient(manifestHttp, 30000);
  const char* manifestHeaders[] = {"Content-Type"};
  manifestHttp.collectHeaders(manifestHeaders, 1);
  if (!manifestHttp.begin(base + String(DEVICE_QUEUE_MANIFEST_PATH))) {
    lastDeviceErrorCode = "DEVICE-QUEUE-URL";
    lastDeviceErrorMessage = "Queue Manifest URL 無法初始化";
    return QueueDownloadResult::Failed;
  }
  manifestHttp.addHeader("Authorization", "Bearer " + cfg.device_token);
  const int status = manifestHttp.GET();
  if (status == HTTP_CODE_NOT_FOUND) {
    manifestHttp.end();
    return QueueDownloadResult::EmptyOrUnsupported;
  }
  const int manifestLength = manifestHttp.getSize();
  const String contentType = manifestHttp.header("Content-Type");
  if (status != HTTP_CODE_OK || manifestLength <= 0
      || manifestLength > static_cast<int>(inktime::kQueueManifestMaxBytes)
      || !contentType.startsWith("application/json")) {
    manifestHttp.end();
    lastDeviceErrorCode = "DEVICE-QUEUE-HTTP";
    lastDeviceErrorMessage = "Queue Manifest HTTP／Content-Type／長度不合法";
    return QueueDownloadResult::Failed;
  }

  JsonDocument manifest;
  const DeserializationError jsonError = deserializeJson(manifest, manifestHttp.getStream());
  manifestHttp.end();
  const JsonVariantConst schema = manifest["schema_version"];
  const JsonVariantConst version = manifest["queue_version"];
  const JsonVariantConst rawItems = manifest["items"];
  if (jsonError || manifest.overflowed() || !schema.is<int32_t>() || schema.is<bool>()
      || schema.as<int32_t>() != 1 || !version.is<int32_t>() || version.is<bool>()
      || version.as<int32_t>() < 0 || !rawItems.is<JsonArrayConst>()) {
    lastDeviceErrorCode = "DEVICE-QUEUE-SCHEMA";
    lastDeviceErrorMessage = "Queue Manifest schema、version 或 items 不合法";
    return QueueDownloadResult::Failed;
  }
  const JsonArrayConst items = rawItems.as<JsonArrayConst>();
  if (items.size() == 0U) return QueueDownloadResult::EmptyOrUnsupported;
  if (items.size() > 14U) {
    lastDeviceErrorCode = "DEVICE-QUEUE-COUNT";
    lastDeviceErrorMessage = "Queue Item 數量超過裝置上限";
    return QueueDownloadResult::Failed;
  }

  String selectedItemId;
  String selectedReleaseId;
  String selectedSha;
  String selectedDownloadUrl;
  String selectedPixelFormat;
  String selectedRenderProfile;
  int64_t selectedSize = 0;
  bool selected = false;
  const bool enhancedOffline = cfg.delivery_mode == "inktime_offline_schedule";
  for (size_t index = 0; index < items.size(); ++index) {
    const JsonVariantConst rawItem = items[index];
    if (!rawItem.is<JsonObjectConst>()) {
      lastDeviceErrorCode = "DEVICE-QUEUE-ITEM";
      lastDeviceErrorMessage = "Queue Item 必須是 JSON object";
      return QueueDownloadResult::Failed;
    }
    const JsonObjectConst item = rawItem.as<JsonObjectConst>();
    const JsonVariantConst rawSize = item["size"];
    const JsonVariantConst rawWidth = item["width"];
    const JsonVariantConst rawHeight = item["height"];
    if (!rawSize.is<int64_t>() || rawSize.is<bool>() || !rawWidth.is<int32_t>()
        || rawWidth.is<bool>() || !rawHeight.is<int32_t>() || rawHeight.is<bool>()) {
      lastDeviceErrorCode = "DEVICE-QUEUE-INTEGER";
      lastDeviceErrorMessage = "Queue size／width／height 必須是真正 JSON integer";
      return QueueDownloadResult::Failed;
    }
    String itemId = item["queue_item_id"] | "";
    String releaseId = item["release_id"] | "";
    String sha = item["sha256"] | "";
    String downloadUrl = item["download_url"] | "";
    String pixelFormat = item["pixel_format"] | "";
    String renderProfile = item["render_profile"] | "";
    const JsonVariantConst rawOfflinePrefetch = item["offline_prefetch_allowed"];
    if (!rawOfflinePrefetch.isNull()
        && (!rawOfflinePrefetch.is<bool>() || rawOfflinePrefetch.is<int>())) {
      lastDeviceErrorCode = "DEVICE-QUEUE-ITEM";
      lastDeviceErrorMessage = "offline_prefetch_allowed 必須是真正 JSON boolean";
      return QueueDownloadResult::Failed;
    }
    const bool offlinePrefetchAllowed = rawOfflinePrefetch | false;
    const String deliveryMode = item["delivery_mode"] | "online_queue";
    const int64_t size = rawSize.as<int64_t>();
    const inktime::QueueItemContract contract = {
      itemId.c_str(), releaseId.c_str(), sha.c_str(), downloadUrl.c_str(), true, size,
    };
    const bool compatibleProfile = renderProfile == "safe_4c"
      || renderProfile == String(INKTIME_PANEL_PROFILE);
    if (!inktime::validQueueItem(contract) || rawWidth.as<int32_t>() != FB_WIDTH
        || rawHeight.as<int32_t>() != FB_HEIGHT
        || (pixelFormat != "2bpp" && pixelFormat != "indexed4")
        || !compatibleProfile) {
      lastDeviceErrorCode = "DEVICE-QUEUE-ITEM";
      lastDeviceErrorMessage = "Queue Item 身分、SHA、路徑、尺寸或 Profile 不合法";
      return QueueDownloadResult::Failed;
    }
    if (enhancedOffline && (deliveryMode != "offline_schedule" || !offlinePrefetchAllowed)) {
      continue;
    }
    if (!selected) {
      selectedItemId = itemId;
      selectedReleaseId = releaseId;
      selectedSha = sha;
      selectedDownloadUrl = downloadUrl;
      selectedPixelFormat = pixelFormat;
      selectedRenderProfile = renderProfile;
      selectedSize = size;
      selected = true;
    }
  }

  if (!selected) return QueueDownloadResult::EmptyOrUnsupported;

  currentFromQueue = true;
  currentQueueItemId = selectedItemId;
  currentQueueVersion = version.as<int32_t>();
  currentReleaseId = selectedReleaseId;
  currentRenderProfile = selectedRenderProfile;
  currentPayloadSha256 = selectedSha;
  currentPayloadShaVerified = false;
  currentPayloadIntegrityTrusted = false;
  currentDisplaySkipped = false;
  if (!sendQueueEvent(cfg, inktime::QueueEvent::ManifestReceived)) {
    return QueueDownloadResult::Failed;
  }

  const bool indexed4 = selectedPixelFormat == "indexed4";
  const size_t packedSize = static_cast<size_t>(FB_WIDTH) * FB_HEIGHT / (indexed4 ? 2U : 4U);
  if (selectedSize != static_cast<int64_t>(packedSize)) {
    lastDeviceErrorCode = "DEVICE-QUEUE-SIZE";
    lastDeviceErrorMessage = "Queue Payload size 與 pixel format 不一致";
    return QueueDownloadResult::Failed;
  }

  uint8_t* packed = nullptr;
#if INKTIME_PHOTOPAINTER_ENABLED
  packed = photoPainter.allocateWireBuffer(packedSize);
#else
  packed = static_cast<uint8_t*>(heap_caps_malloc(packedSize, MALLOC_CAP_8BIT | MALLOC_CAP_SPIRAM));
  if (!packed) packed = static_cast<uint8_t*>(heap_caps_malloc(packedSize, MALLOC_CAP_8BIT));
#endif
  if (!packed) {
    lastDeviceErrorCode = "DEVICE-MEMORY";
    lastDeviceErrorMessage = "無法配置 Queue 下載緩衝區";
    return QueueDownloadResult::Failed;
  }
  if (!sendQueueEvent(cfg, inktime::QueueEvent::DownloadStarted)) {
    heap_caps_free(packed);
    return QueueDownloadResult::Failed;
  }

  HTTPClient fileHttp;
  configureHttpClient(fileHttp, 60000);
  const char* fileHeaders[] = {"Content-Type"};
  fileHttp.collectHeaders(fileHeaders, 1);
  if (!fileHttp.begin(base + selectedDownloadUrl)) {
    heap_caps_free(packed);
    lastDeviceErrorCode = "DEVICE-QUEUE-FILE-URL";
    lastDeviceErrorMessage = "Queue download URL 無法初始化";
    return QueueDownloadResult::Failed;
  }
  fileHttp.addHeader("Authorization", "Bearer " + cfg.device_token);
  const int fileStatus = fileHttp.GET();
  const String fileContentType = fileHttp.header("Content-Type");
  if (fileStatus != HTTP_CODE_OK || fileHttp.getSize() != static_cast<int>(packedSize)
      || !fileContentType.startsWith("application/octet-stream")) {
    fileHttp.end();
    heap_caps_free(packed);
    lastDeviceErrorCode = "DEVICE-QUEUE-DOWNLOAD";
    lastDeviceErrorMessage = "Queue download HTTP／Content-Type／長度不合法";
    return QueueDownloadResult::Failed;
  }
  WiFiClient* stream = fileHttp.getStreamPtr();
  size_t total = 0U;
  const uint32_t started = millis();
  while (total < packedSize && millis() - started < 60000U) {
    const size_t available = stream->available();
    if (available == 0U) {
      if (!fileHttp.connected()) break;
      delay(1);
      continue;
    }
    const size_t count = min(available, packedSize - total);
    const int received = stream->read(packed + total, count);
    if (received > 0) total += static_cast<size_t>(received);
  }
  fileHttp.end();
  if (total != packedSize) {
    heap_caps_free(packed);
    lastDeviceErrorCode = "DEVICE-QUEUE-DOWNLOAD";
    lastDeviceErrorMessage = "Queue Payload 未完整下載";
    return QueueDownloadResult::Failed;
  }
  if (!sendQueueEvent(cfg, inktime::QueueEvent::DownloadCompleted)) {
    heap_caps_free(packed);
    return QueueDownloadResult::Failed;
  }

  unsigned char digest[32];
  if (calculateSha256(packed, packedSize, digest) != 0) {
    heap_caps_free(packed);
    lastDeviceErrorCode = "DEVICE-QUEUE-HASH";
    lastDeviceErrorMessage = "Queue Payload SHA-256 計算失敗";
    return QueueDownloadResult::Failed;
  }
  char actualSha[65];
  for (size_t index = 0; index < 32U; ++index) {
    snprintf(actualSha + index * 2U, 3U, "%02x", digest[index]);
  }
  actualSha[64] = '\0';
  if (!selectedSha.equalsIgnoreCase(String(actualSha))) {
    heap_caps_free(packed);
    lastDeviceErrorCode = "DEVICE-QUEUE-HASH";
    lastDeviceErrorMessage = "Queue Payload SHA-256 不一致";
    return QueueDownloadResult::Failed;
  }
  currentPayloadShaVerified = true;
  if (!sendQueueEvent(cfg, inktime::QueueEvent::HashVerified)) {
    heap_caps_free(packed);
    return QueueDownloadResult::Failed;
  }

  if (frameData) heap_caps_free(frameData);
#if INKTIME_PHOTOPAINTER_ENABLED
  const uint32_t sourceHash = inktime::sourceHash32(selectedSha.c_str());
  const inktime::DisplayRotation rotation = cfg.rotate180
    ? inktime::DisplayRotation::Rotate180
    : inktime::DisplayRotation::Rotate0;
  uint8_t* nativeFrame = nullptr;
  if (!photoPainter.loadCachedFrame(sourceHash, rotation, &nativeFrame, selectedSha.c_str())
      && !photoPainter.convertAndCache(
        packed, packedSize, indexed4, sourceHash, rotation, &nativeFrame, selectedSha.c_str())) {
    heap_caps_free(packed);
    frameData = nullptr;
    lastDeviceErrorCode = photoPainter.lastError();
    lastDeviceErrorMessage = "PhotoPainter Queue framebuffer 轉換失敗";
    return QueueDownloadResult::Failed;
  }
  heap_caps_free(packed);
  frameData = nativeFrame;
  frameDataSize = inktime::kPhotoPainterFrameBytes;
  frameIndexed4 = true;
  frameNativePalette = true;
  if (enhancedOffline && !photoPainter.writeFormalFrame(
        selectedSha.c_str(), rotation, nativeFrame, inktime::kPhotoPainterFrameBytes)) {
    heap_caps_free(nativeFrame);
    frameData = nullptr;
    frameDataSize = 0;
    lastDeviceErrorCode = "DEVICE-OFFLINE-FRAME";
    lastDeviceErrorMessage = "Enhanced Formal Frame 無法原子寫入 SD";
    return QueueDownloadResult::Failed;
  }
#else
  frameData = packed;
  frameDataSize = packedSize;
  frameIndexed4 = indexed4;
  frameNativePalette = false;
#endif
  currentPayloadIntegrityTrusted = true;
  currentDisplaySkipped = shouldSkipCurrentDisplay(cfg);
  return QueueDownloadResult::Used;
}

static bool offlinePrefetchWake(const Config &cfg, time_t nowEpoch) {
  if (cfg.delivery_mode != "inktime_offline_schedule"
      || cfg.schedule_count == 0 || cfg.prefetch_lead_minutes == 0 || nowEpoch <= 0) {
    return false;
  }
  struct tm localNow = {};
  localtime_r(&nowEpoch, &localNow);
  time_t nextDisplay = 0;
  for (int dayOffset = 0; dayOffset <= 1; ++dayOffset) {
    for (uint8_t index = 0; index < cfg.schedule_count; ++index) {
      struct tm candidate = localNow;
      candidate.tm_sec = 0;
      candidate.tm_min = cfg.schedule_slots[index].minute;
      candidate.tm_hour = cfg.schedule_slots[index].hour;
      candidate.tm_mday += dayOffset;
      const time_t candidateEpoch = mktime(&candidate);
      if (candidateEpoch > nowEpoch
          && (nextDisplay == 0 || candidateEpoch < nextDisplay)) {
        nextDisplay = candidateEpoch;
      }
    }
  }
  if (nextDisplay <= nowEpoch) return false;
  const time_t lead = static_cast<time_t>(cfg.prefetch_lead_minutes) * 60;
  return nowEpoch >= nextDisplay - lead && nowEpoch < nextDisplay;
}

#if INKTIME_PHOTOPAINTER_ENABLED
static bool loadOfflineScheduledLocalFrame(const Config &cfg, time_t nowEpoch);
#endif

bool downloadDailyPhotoBin(Config &cfg) {
  currentPrefetchOnly = false;
  if (!resumePendingQueueAck(cfg)) return false;
#if INKTIME_PHOTOPAINTER_ENABLED
  const bool timerRequestedNetwork = enhancedNetworkWakeRequested;
  enhancedNetworkWakeRequested = false;
  if (cfg.delivery_mode == "inktime_offline_schedule"
      && (timerRequestedNetwork || offlinePrefetchWake(cfg, time(nullptr)))) {
    const bool displayAtThisWake = cfg.prefetch_lead_minutes == 0U;
    currentPrefetchOnly = !displayAtThisWake;
    const bool prefetched = downloadOfflineScheduleAndFrames(cfg);
    if (prefetched && displayAtThisWake) {
      currentPrefetchOnly = false;
      return loadOfflineScheduledLocalFrame(cfg, time(nullptr));
    }
    return prefetched;
  }
#endif
  const QueueDownloadResult queueResult = downloadQueuePhotoBin(cfg);
  if (queueResult == QueueDownloadResult::Used) return true;
  if (queueResult == QueueDownloadResult::Failed) {
    if (currentFromQueue && !loadPendingQueueAck().valid) {
      sendQueueEvent(cfg, inktime::QueueEvent::DisplayFailed, false, lastDeviceErrorCode);
    }
    return false;
  }
  const bool latest = downloadLatestPhotoBin(cfg);
  if (latest) currentDisplaySkipped = shouldSkipCurrentDisplay(cfg);
  return latest;
}

void reportDeviceStatus(const Config &cfg, bool displayUpdated) {
  if (WiFi.status() != WL_CONNECTED || cfg.backend_hostport.length() == 0 || cfg.device_token.length() == 0) return;
  String base;
  if (!normalizedBackendBase(cfg, base)) return;

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
  payload["display_skipped"] = currentDisplaySkipped;
  if (currentDisplaySkipped) payload["display_skip_reason"] = "same_sha256";
  payload["payload_sha256_verified"] = currentPayloadShaVerified;
  payload["applied_config_version"] = cfg.config_version;
  payload["panel_profile"] = INKTIME_PANEL_PROFILE;
  payload["render_profile"] = currentRenderProfile;
  payload["release_id"] = currentReleaseId;
  payload["error_code"] = lastDeviceErrorCode;
  payload["error_message"] = lastDeviceErrorMessage;
#if INKTIME_ALLOW_INSECURE_DEVICE_HTTP
  payload["transport_profile"] = "trusted-lan-http";
  payload["transport_security_state"] = "degraded";
  payload["transport_warning"] = "no_tls";
#else
  payload["transport_profile"] = "https-only";
  payload["transport_security_state"] = "secure-required";
#endif
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
  payload["button_wakeup"] = photoPainter.wokeFromUserButton();
#endif
  payload["last_refresh_duration_ms"] = lastRefreshDurationMs;
  payload["wake_duration_ms"] = millis();
  String body;
  serializeJson(payload, body);

  HTTPClient statusHttp;
  configureHttpClient(statusHttp, 15000);
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
  const bool updated = photoPainter.displayFrame(frameData, frameDataSize);
  lastRefreshDurationMs = photoPainter.lastRefreshDurationMs();
  return updated;
#else

  const uint32_t refreshStarted = millis();
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

  lastRefreshDurationMs = millis() - refreshStarted;
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

  if (cfg.delivery_mode == "inktime_offline_schedule"
      && cfg.schedule_count > 0 && cfg.schedule_count <= inktime::kMaxOfflineSlots) {
    struct tm localNow = now;
    const time_t nowEpoch = mktime(&localNow);
    time_t nextDisplay = 0;
    for (int dayOffset = 0; dayOffset <= 1; ++dayOffset) {
      for (uint8_t index = 0; index < cfg.schedule_count; ++index) {
        struct tm candidate = localNow;
        candidate.tm_sec = 0;
        candidate.tm_min = cfg.schedule_slots[index].minute;
        candidate.tm_hour = cfg.schedule_slots[index].hour;
        candidate.tm_mday += dayOffset;
        const time_t candidateEpoch = mktime(&candidate);
        if (candidateEpoch > nowEpoch && (nextDisplay == 0 || candidateEpoch < nextDisplay)) {
          nextDisplay = candidateEpoch;
        }
      }
    }
    if (nextDisplay > nowEpoch) {
      const uint64_t leadSeconds = static_cast<uint64_t>(cfg.prefetch_lead_minutes) * 60ULL;
      const time_t prefetchEpoch = nextDisplay > static_cast<time_t>(leadSeconds)
        ? nextDisplay - static_cast<time_t>(leadSeconds)
        : nextDisplay;
      const time_t wakeEpoch = prefetchEpoch > nowEpoch ? prefetchEpoch : nextDisplay;
      goDeepSleepUntilEpoch(nowEpoch, wakeEpoch);
      return;
    }
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

#if INKTIME_PHOTOPAINTER_ENABLED
static bool parseOfflineLocalDate(const String &value, struct tm &output) {
  if (value.length() != 10 || value[4] != '-' || value[7] != '-') return false;
  const int year = value.substring(0, 4).toInt();
  const int month = value.substring(5, 7).toInt();
  const int day = value.substring(8, 10).toInt();
  if (year < 2020 || year > 2200 || month < 1 || month > 12 || day < 1 || day > 31) {
    return false;
  }
  output = {};
  output.tm_year = year - 1900;
  output.tm_mon = month - 1;
  output.tm_mday = day;
  output.tm_isdst = -1;
  return true;
}

static bool loadOfflineScheduledLocalFrame(const Config &cfg, time_t nowEpoch) {
  // Enhanced local mode is deliberately cache-only.  It never calls Wi-Fi,
  // NTP, Manifest, or status endpoints; a missing formal frame is a safe
  // no-refresh result instead of a network fallback.
  if (nowEpoch <= 0) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-FRAME";
    lastDeviceErrorMessage = "離線排程沒有可驗證的 RTC 時間";
    return false;
  }
  String activeJson;
  if (!photoPainter.readActiveSchedule(activeJson)) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE";
    lastDeviceErrorMessage = "離線排程 active schedule 不存在或無法讀取";
    return false;
  }
  JsonDocument active;
  const DeserializationError jsonError = deserializeJson(active, activeJson);
  const JsonVariantConst rawSlots = active["slots"];
  String targetDate = active["target_local_date"] | "";
  if (targetDate.length() == 0U) targetDate = active["target_date"] | "";
  if (jsonError || active.overflowed() || !rawSlots.is<JsonArrayConst>()
      || targetDate.length() != 10) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE";
    lastDeviceErrorMessage = "離線排程 active schedule schema 不合法";
    return false;
  }
  struct tm targetLocal = {};
  if (!parseOfflineLocalDate(targetDate, targetLocal)) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE";
    lastDeviceErrorMessage = "離線排程 target date 不合法";
    return false;
  }
  struct tm nowLocal = {};
  localtime_r(&nowEpoch, &nowLocal);
  if (nowLocal.tm_year != targetLocal.tm_year
      || nowLocal.tm_mon != targetLocal.tm_mon
      || nowLocal.tm_mday != targetLocal.tm_mday) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE";
    lastDeviceErrorMessage = "離線排程不是目前本地日期";
    return false;
  }
  const JsonArrayConst slots = rawSlots.as<JsonArrayConst>();
  int selectedIndex = -1;
  time_t selectedEpoch = 0;
  String selectedSha;
  String selectedRelease;
  String selectedProfile;
  String selectedQueueItemId;
  int32_t selectedQueueVersion = -1;
  for (size_t index = 0; index < slots.size(); ++index) {
    const JsonVariantConst rawSlot = slots[index];
    if (!rawSlot.is<JsonObjectConst>()) continue;
    const JsonObjectConst slot = rawSlot.as<JsonObjectConst>();
    const int slotIndex = slot["slot_index"] | -1;
    if (slotIndex < 0 || slotIndex >= cfg.schedule_count) continue;
    struct tm candidate = targetLocal;
    candidate.tm_hour = cfg.schedule_slots[slotIndex].hour;
    candidate.tm_min = cfg.schedule_slots[slotIndex].minute;
    candidate.tm_sec = 0;
    const time_t candidateEpoch = mktime(&candidate);
    const String sha = slot["sha256"] | "";
    if (candidateEpoch <= nowEpoch && candidateEpoch >= selectedEpoch
        && inktime::isSha256Hex(sha.c_str())) {
      selectedIndex = slotIndex;
      selectedEpoch = candidateEpoch;
      selectedSha = sha;
      selectedRelease = slot["release_id"] | "";
      selectedProfile = slot["render_profile"] | "";
      selectedQueueItemId = slot["queue_item_id"] | "";
      selectedQueueVersion = slot["queue_version"] | -1;
    }
  }
  if (selectedIndex < 0 || !inktime::isSha256Hex(selectedSha.c_str())) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-FRAME";
    lastDeviceErrorMessage = "目前離線排程 Slot 沒有可驗證的正式 Frame";
    return false;
  }
  const inktime::DisplayRotation rotation = cfg.rotate180
    ? inktime::DisplayRotation::Rotate180
    : inktime::DisplayRotation::Rotate0;
  currentReleaseId = selectedRelease;
  currentRenderProfile = selectedProfile;
  currentQueueItemId = selectedQueueItemId;
  currentQueueVersion = selectedQueueVersion;
  currentFromQueue = inktime::boundedText(
      currentQueueItemId.c_str(), inktime::kQueueIdentifierMaxBytes)
    && currentQueueVersion >= 0;
  uint8_t* localFrame = nullptr;
  if (!photoPainter.loadFormalFrame(selectedSha.c_str(), rotation, &localFrame)) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-FRAME";
    lastDeviceErrorMessage = "離線排程正式 Frame 不存在或完整性驗證失敗";
    return false;
  }
  if (frameData) heap_caps_free(frameData);
  frameData = localFrame;
  frameDataSize = inktime::kPhotoPainterFrameBytes;
  frameIndexed4 = true;
  frameNativePalette = true;
  currentPayloadSha256 = selectedSha;
  currentPayloadShaVerified = true;
  currentPayloadIntegrityTrusted = true;
  currentDisplaySkipped = shouldSkipCurrentDisplay(cfg);
  return true;
}

static void runOfflineLocalCycle() {
  time_t rtcEpoch = 0;
  struct tm offlineTime = {};
  const bool hasOfflineTime = photoPainter.readRtc(rtcEpoch);
  if (hasOfflineTime) {
    struct timeval value = {rtcEpoch, 0};
    settimeofday(&value, nullptr);
    applyFixedTimezoneWithoutNtp(g_cfg.tz_offset_minutes);
    localtime_r(&rtcEpoch, &offlineTime);
  }
  const bool ok = loadOfflineScheduledLocalFrame(g_cfg, hasOfflineTime ? rtcEpoch : 0);
  bool displayUpdated = false;
  if (ok) {
    if (currentDisplaySkipped) {
      displayUpdated = true;
    } else {
      initDisplay(g_cfg);
      displayUpdated = drawFromFrameData(g_cfg);
    }
    if (displayUpdated) saveDisplayRecord(g_cfg, true);
    if (currentFromQueue) {
      if (displayUpdated) {
        sendQueueEvent(
          g_cfg,
          inktime::QueueEvent::DisplayCompleted,
          currentDisplaySkipped,
          String(""),
          true
        );
      } else {
        sendQueueEvent(
          g_cfg,
          inktime::QueueEvent::DisplayFailed,
          false,
          photoPainter.lastError(),
          true
        );
      }
    }
  } else if (currentFromQueue) {
    // A missing/corrupt formal frame still gets a durable terminal outcome;
    // the queue reservation must not be silently left without a display ACK.
    sendQueueEvent(
      g_cfg,
      inktime::QueueEvent::DisplayFailed,
      false,
      photoPainter.lastError(),
      true
    );
  }
  if (!displayUpdated && ok) {
    lastDeviceErrorCode = photoPainter.lastError();
    lastDeviceErrorMessage = "離線排程電子紙刷新失敗或逾時";
    saveDisplayRecord(g_cfg, false);
  }
  // There is intentionally no reportDeviceStatus() here: that endpoint is a
  // network call and enhanced local mode must remain fully offline.
  sleepUntilNextSchedule(g_cfg, hasOfflineTime, offlineTime);
}
#endif

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
#if INKTIME_ALLOW_INSECURE_DEVICE_HTTP
  DBG_PRINTLN("[SECURITY] trusted-LAN HTTP build：僅限可信任 LAN／IoT VLAN，沒有 TLS");
#endif
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

#if INKTIME_PHOTOPAINTER_ENABLED
  const esp_sleep_wakeup_cause_t wakeCause = esp_sleep_get_wakeup_cause();
  const bool timerWake = wakeCause == ESP_SLEEP_WAKEUP_TIMER;
  if (g_cfg.delivery_mode == "inktime_offline_schedule" && timerWake
      && !photoPainter.forceNetworkRefresh()) {
    time_t rtcEpoch = 0;
    if (!photoPainter.readRtc(rtcEpoch)) {
      runOfflineLocalCycle();
      return;
    }
    applyFixedTimezoneWithoutNtp(g_cfg.tz_offset_minutes);
    struct timeval value = {rtcEpoch, 0};
    settimeofday(&value, nullptr);
    const bool networkWake = g_cfg.prefetch_lead_minutes == 0U
      || offlinePrefetchWake(g_cfg, rtcEpoch);
    if (!networkWake) {
      runOfflineLocalCycle();
      return;
    }
    enhancedNetworkWakeRequested = true;
  }
#endif

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
      applyFixedTimezoneWithoutNtp(g_cfg.tz_offset_minutes);
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
    if (currentPrefetchOnly) {
      displayUpdated = false;
    } else {
    bool mayDisplay = true;
    if (currentDisplaySkipped) {
      saveDisplayRecord(g_cfg, true);
      if (currentFromQueue) {
        mayDisplay = sendQueueEvent(g_cfg, inktime::QueueEvent::DisplayCompleted, true);
      }
    } else if (currentFromQueue) {
      mayDisplay = sendQueueEvent(g_cfg, inktime::QueueEvent::DisplayStarted);
    }
    if (mayDisplay && !currentDisplaySkipped) {
      initDisplay(g_cfg);
      displayUpdated = drawFromFrameData(g_cfg);
    }
    if (displayUpdated) {
      saveDisplayRecord(g_cfg, true);
      if (currentFromQueue) {
        sendQueueEvent(g_cfg, inktime::QueueEvent::DisplayCompleted);
      }
    } else if (!currentDisplaySkipped && mayDisplay) {
#if INKTIME_PHOTOPAINTER_ENABLED
      lastDeviceErrorCode = photoPainter.lastError();
#else
      lastDeviceErrorCode = "DEVICE-DISPLAY";
#endif
      lastDeviceErrorMessage = "電子紙刷新失敗或逾時";
      saveDisplayRecord(g_cfg, false);
      if (currentFromQueue && !loadPendingQueueAck().valid) {
        sendQueueEvent(g_cfg, inktime::QueueEvent::DisplayFailed, false, lastDeviceErrorCode);
      }
    }
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
