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
#include "device_config_store.h"
#include "queue_client_core.h"
#include "queue_runtime_types.h"
#include "device_http_transport.h"
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

// Secure builds require a compile-time or portal-provisioned trust anchor;
// isolated LAN HTTP remains available only in an explicit development build.
#ifndef INKTIME_ALLOW_INSECURE_DEVICE_HTTP
#define INKTIME_ALLOW_INSECURE_DEVICE_HTTP 0
#endif

// =======================
//  配置存储 / WiFi / WebServer
// =======================
Preferences prefs;
WebServer  server(80);
inktime::DeviceConfigStore configStore;

struct Config {
  String  wifi_ssid;
  String  wifi_pass;
  String  backend_hostport;
  String  ca_pem;
  String  device_token;
  int32_t tz_offset_minutes;
  uint8_t refresh_hour;
  uint8_t refresh_minute;
  bool    rotate180;
#if INKTIME_PHOTOPAINTER_ENABLED
  inktime::OfflineSlot schedule_slots[inktime::kMaxOfflineSlots];
  uint8_t schedule_count;
  uint16_t prefetch_lead_minutes;
  String  delivery_mode;
  String  button_wake_action;
#endif
  uint32_t config_version;
  bool    valid;
};

const char*  DEFAULT_HOSTPORT = "";
const int32_t DEFAULT_TZ_MINUTES = 8 * 60;
const uint8_t DEFAULT_HOUR    = 8;
const uint8_t DEFAULT_MINUTE  = 0;
#if INKTIME_PHOTOPAINTER_ENABLED
const uint16_t DEFAULT_PREFETCH_LEAD_MINUTES = 5;

static bool parseOfflineClock(const String &value, inktime::OfflineSlot &slot) {
  if (value.length() != 5 || value[2] != ':') return false;
  if (value[0] < '0' || value[0] > '9' || value[1] < '0' || value[1] > '9'
      || value[3] < '0' || value[3] > '9' || value[4] < '0' || value[4] > '9') return false;
  slot.hour = static_cast<uint8_t>((value[0] - '0') * 10 + value[1] - '0');
  slot.minute = static_cast<uint8_t>((value[3] - '0') * 10 + value[4] - '0');
  return inktime::validOfflineSlot(slot);
}

static bool nextIsoLocalDate(const String &value, String &output) {
  output = "";
  if (value.length() != 10U || value[4] != '-' || value[7] != '-') return false;
  for (uint8_t index = 0; index < 10U; ++index) {
    if (index == 4U || index == 7U) continue;
    if (value[index] < '0' || value[index] > '9') return false;
  }
  int year = value.substring(0, 4).toInt();
  int month = value.substring(5, 7).toInt();
  int day = value.substring(8, 10).toInt();
  if (year < 2000 || month < 1 || month > 12 || day < 1) return false;
  const bool leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
  const uint8_t days[] = {0, 31, static_cast<uint8_t>(leap ? 29 : 28), 31, 30, 31, 30,
    31, 31, 30, 31, 30, 31};
  if (day > days[month]) return false;
  ++day;
  if (day > days[month]) {
    day = 1;
    ++month;
    if (month > 12) {
      month = 1;
      ++year;
    }
  }
  char next[11] = {0};
  snprintf(next, sizeof(next), "%04d-%02d-%02d", year, month, day);
  output = String(next);
  return true;
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

#endif

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
#if INKTIME_PHOTOPAINTER_ENABLED
bool offlineScheduleTxnBlocked = false;
#endif
String portalSetupSecret;
String portalNonce;
String portalApSsid;
String portalApPassword;
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

#if INKTIME_PHOTOPAINTER_ENABLED
static bool reconcilePendingScheduleConfigTransaction(Config &cfg);
#endif

static void setConfigPersistenceError(const String &persistError) {
  lastDeviceErrorCode = "DEVICE-CONFIG-PERSIST";
  lastDeviceErrorMessage = "遠端設定持久化失敗：";
  if (persistError.length() > 0U) {
    lastDeviceErrorMessage += persistError;
  } else {
    lastDeviceErrorMessage += "PAIRING-NVS-UNKNOWN";
  }
}

static int calculateSha256(const unsigned char* input, size_t length, unsigned char output[32]) {
#if MBEDTLS_VERSION_MAJOR >= 3
  return mbedtls_sha256(input, length, output, 0);
#else
  return mbedtls_sha256_ret(input, length, output, 0);
#endif
}

static bool backendTransportAllowed(const String &base, const String &ca_pem) {
  String errorCode;
  if (inktime::DeviceHttpTransport::backendUrlAllowed(base, ca_pem, errorCode)) return true;
  lastDeviceErrorCode = errorCode;
  lastDeviceErrorMessage = "Backend URL 或 TLS trust anchor 不符合安全政策";
  return false;
}

static String randomPortalSecret() {
  char value[25];
  for (uint8_t i = 0; i < 12; ++i) snprintf(value + i * 2, 3, "%02X", static_cast<unsigned>(esp_random() & 0xff));
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
  String storeError;
  (void)configStore.clearAll(storeError);
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
static bool loadOfflineRetryState(
  uint8_t &attemptOut, int64_t &epochOut, int64_t &nextSlotOut) {
  prefs.begin("dashcfg", true);
  const uint8_t storedAttempt = prefs.getUChar("offretry_attempt", 0U);
  const int64_t storedEpoch = prefs.getLong64("offretry_epoch", 0);
  const int64_t storedNextSlot = prefs.getLong64("offretry_next", 0);
  prefs.end();
  attemptOut = storedAttempt > 2U ? 2U : storedAttempt;
  epochOut = storedEpoch;
  nextSlotOut = storedNextSlot;
  return storedEpoch > 0;
}

static void saveOfflineRetryState(uint8_t attempt, int64_t epoch, int64_t nextSlotEpoch) {
  prefs.begin("dashcfg", false);
  prefs.putUChar("offretry_attempt", attempt > 2U ? 2U : attempt);
  prefs.putLong64("offretry_epoch", epoch);
  prefs.putLong64("offretry_next", nextSlotEpoch > 0 ? nextSlotEpoch : 0);
  prefs.end();
}

static void clearOfflineRetryState() {
  prefs.begin("dashcfg", false);
  prefs.remove("offretry_attempt");
  prefs.remove("offretry_epoch");
  prefs.remove("offretry_next");
  prefs.end();
}

static time_t scheduleOfflineRecovery(
  time_t nowEpoch, int64_t serverRetryEpoch = 0, int64_t nextSlotEpoch = 0) {
  if (nowEpoch <= 0) return 0;
  uint8_t attempt = 0U;
  int64_t storedEpoch = 0;
  int64_t storedNextSlot = 0;
  loadOfflineRetryState(attempt, storedEpoch, storedNextSlot);
  if (serverRetryEpoch <= 0 && inktime::validOfflineRetryEpoch(
        static_cast<uint64_t>(nowEpoch), storedEpoch, storedNextSlot)) {
    return static_cast<time_t>(storedEpoch);
  }
  const inktime::OfflineRetryPlan plan = inktime::buildOfflineRetryPlan(
    static_cast<uint64_t>(nowEpoch), attempt, serverRetryEpoch, nextSlotEpoch);
  saveOfflineRetryState(
    plan.nextAttempt,
    static_cast<int64_t>(plan.sleepUntilEpoch),
    nextSlotEpoch > 0 ? nextSlotEpoch : 0);
  return static_cast<time_t>(plan.sleepUntilEpoch);
}
#endif

#if INKTIME_PHOTOPAINTER_ENABLED
// Preferences keys are kept under the ESP32 NVS key-size limit; these two
// bounded keys represent preview_schedule_id and preview_slot_index.
static int16_t loadPreviewCursorForSchedule(const String &scheduleId) {
  prefs.begin("dashcfg", true);
  const String storedScheduleId = prefs.getString("preview_sched", "");
  const int32_t storedIndex = prefs.getInt("preview_idx", -1);
  prefs.end();
  if (storedScheduleId != scheduleId) {
    prefs.begin("dashcfg", false);
    prefs.putString("preview_sched", scheduleId);
    prefs.putInt("preview_idx", -1);
    prefs.end();
    return -1;
  }
  return storedIndex < 0 || storedIndex > 127 ? -1 : static_cast<int16_t>(storedIndex);
}

static void savePreviewCursor(const String &scheduleId, int16_t slotIndex) {
  prefs.begin("dashcfg", false);
  prefs.putString("preview_sched", scheduleId);
  prefs.putInt("preview_idx", slotIndex);
  prefs.end();
}
#endif

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
    && pending.eventEpoch >= 0
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
    journal.getLong64(ackJournalKey('t', index).c_str(), 0),
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
  journal.putLong64(ackJournalKey('t', index).c_str(), pending.eventEpoch);
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
    journal.remove(ackJournalKey('t', last).c_str());
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
    0,
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
static inktime::configstore::ConfigPayload configPayload(const Config &cfg) {
  inktime::configstore::ConfigPayload payload;
  payload.wifi_ssid = cfg.wifi_ssid.c_str();
  payload.wifi_pass = cfg.wifi_pass.c_str();
  payload.backend_hostport = cfg.backend_hostport.c_str();
  payload.ca_pem = cfg.ca_pem.c_str();
  payload.device_token = cfg.device_token.c_str();
  payload.tz_offset_minutes = cfg.tz_offset_minutes;
  payload.refresh_hour = cfg.refresh_hour;
  payload.refresh_minute = cfg.refresh_minute;
  payload.rotate180 = cfg.rotate180;
  payload.config_version = cfg.config_version;
#if INKTIME_PHOTOPAINTER_ENABLED
  payload.schedule_count = cfg.schedule_count;
  payload.prefetch_lead_minutes = cfg.prefetch_lead_minutes;
  payload.delivery_mode = cfg.delivery_mode.c_str();
  payload.button_wake_action = cfg.button_wake_action.c_str();
  for (uint8_t index = 0U; index < inktime::configstore::kMaxConfigSlots; ++index) {
    payload.schedule_slots[index] = {
      cfg.schedule_slots[index].hour,
      cfg.schedule_slots[index].minute,
    };
  }
#else
  payload.schedule_count = 1U;
  payload.schedule_slots[0] = {cfg.refresh_hour, cfg.refresh_minute};
  payload.delivery_mode = "legacy_online";
  payload.button_wake_action = "check_new";
#endif
  return payload;
}

static void applyConfigPayload(const inktime::configstore::ConfigPayload &payload, Config &cfg) {
  cfg.wifi_ssid = payload.wifi_ssid.c_str();
  cfg.wifi_pass = payload.wifi_pass.c_str();
  cfg.backend_hostport = payload.backend_hostport.c_str();
  cfg.ca_pem = payload.ca_pem.c_str();
  cfg.device_token = payload.device_token.c_str();
  cfg.tz_offset_minutes = payload.tz_offset_minutes;
  cfg.refresh_hour = payload.refresh_hour;
  cfg.refresh_minute = payload.refresh_minute;
  cfg.rotate180 = payload.rotate180;
  cfg.config_version = payload.config_version;
#if INKTIME_PHOTOPAINTER_ENABLED
  cfg.schedule_count = payload.schedule_count;
  cfg.prefetch_lead_minutes = payload.prefetch_lead_minutes;
  cfg.delivery_mode = payload.delivery_mode.c_str();
  cfg.button_wake_action = payload.button_wake_action.c_str();
  for (uint8_t index = 0U; index < inktime::kMaxOfflineSlots; ++index) {
    cfg.schedule_slots[index] = {
      payload.schedule_slots[index].hour,
      payload.schedule_slots[index].minute,
    };
  }
#endif
  cfg.valid = cfg.wifi_ssid.length() > 0U;
}

static void setConfigDefaults(Config &cfg) {
  cfg = Config{};
  cfg.backend_hostport = DEFAULT_HOSTPORT;
  cfg.tz_offset_minutes = DEFAULT_TZ_MINUTES;
  cfg.refresh_hour = DEFAULT_HOUR;
  cfg.refresh_minute = DEFAULT_MINUTE;
  cfg.rotate180 = false;
  cfg.config_version = 0U;
#if INKTIME_PHOTOPAINTER_ENABLED
  cfg.schedule_count = 1U;
  cfg.schedule_slots[0] = {DEFAULT_HOUR, DEFAULT_MINUTE};
  for (uint8_t index = 1U; index < inktime::kMaxOfflineSlots; ++index) {
    cfg.schedule_slots[index] = {0U, 0U};
  }
  cfg.prefetch_lead_minutes = DEFAULT_PREFETCH_LEAD_MINUTES;
  cfg.delivery_mode = "legacy_online";
  cfg.button_wake_action = "check_new";
#endif
}

void loadConfig(Config &cfg) {
  setConfigDefaults(cfg);
  inktime::configstore::ConfigPayload payload;
  String loadError;
  if (configStore.load(payload, loadError)) {
    applyConfigPayload(payload, cfg);
  } else if (loadError.length() > 0U) {
    setConfigPersistenceError(loadError);
  }
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

bool saveConfig(const Config &cfg, String *errorCodeOut = nullptr) {
  if (errorCodeOut != nullptr) *errorCodeOut = "";
  auto setError = [errorCodeOut](const char *code) {
    if (errorCodeOut != nullptr) *errorCodeOut = code;
  };
  if (cfg.ca_pem.length() > inktime::kMaxDeviceCaPemBytes
      || (cfg.ca_pem.length() > 0 && !inktime::DeviceHttpTransport::trustAnchorValid(cfg.ca_pem))) {
    setError("PAIRING-NVS-001");
    return false;
  }
  const inktime::configstore::ConfigPayload payload = configPayload(cfg);
  String storeError;
  if (!configStore.save(payload, storeError)) {
    if (storeError == "PAIRING-NVS-002") {
      setError("PAIRING-NVS-002");
    } else if (storeError == "PAIRING-NVS-003") {
      setError("PAIRING-NVS-003");
    } else {
      setError(storeError.length() > 0U ? storeError.c_str() : "PAIRING-NVS-001");
    }
    return false;
  }

#if DEBUG_LOG
  DBG_PRINTLN("[CFG] saved");
#endif
  setError("");
  return true;
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
  String caPem   = htmlEscape(g_cfg.ca_pem);
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
  if (portalApSsid.length() > 0 && portalApPassword.length() > 0) {
    html += F("<p><strong>本次 AP 配對資訊（5 分鐘有效）</strong><br>SSID: <code>");
    html += htmlEscape(portalApSsid);
    html += F("</code><br>密碼: <code>");
    html += htmlEscape(portalApPassword);
    html += F("</code><br>設定網址: <code>http://192.168.4.1/</code></p>");
  }
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

  html += F("TLS Root CA PEM（HTTPS 必填；可由編譯 provisioning 或此頁寫入）：<br><textarea name='ca_pem' rows='8' cols='60' maxlength='");
  html += String(inktime::kMaxDeviceCaPemBytes);
  html += F("'>");
  html += caPem;
  html += F("</textarea><br><small>只接受 -----BEGIN CERTIFICATE----- 至 -----END CERTIFICATE-----；CA 不是 secret，但不會寫入狀態回報。</small><br><br>");

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
  String caPem    = server.arg("ca_pem");
  String deviceToken = server.arg("device_token");
  String hourStr  = server.arg("hour");
  String minuteStr = server.arg("minute");
  String tzStr    = server.arg("tz");
  bool rot180Req  = (server.arg("rot180") == "1");

  ssid.trim();
  host.trim();
  caPem.trim();
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
  const bool caProvided = caPem.length() > 0;
  if (caPem.length() > inktime::kMaxDeviceCaPemBytes) {
    server.send(
      400,
      "text/plain; charset=utf-8",
      String("PAIRING-002 Root CA PEM 超過 ") + String(inktime::kMaxDeviceCaPemBytes) + " bytes"
    );
    return;
  }
  if (caProvided && !inktime::DeviceHttpTransport::trustAnchorValid(caPem)) {
    server.send(400, "text/plain; charset=utf-8", "PAIRING-003 Root CA PEM 格式不合法");
    return;
  }
  if (ssid.length() > 32 || pass.length() > 63 || host.length() > 240 || deviceToken.length() > 256
      || host.indexOf('@') >= 0 || !allowedScheme || unsafeOrigin) {
    server.send(400, "text/plain; charset=utf-8", "PAIRING-002 設定格式或長度不合法");
    return;
  }

  Config newCfg = g_cfg;

  if (ssid.length() > 0) newCfg.wifi_ssid = ssid;
  if (pass.length() > 0) newCfg.wifi_pass = pass;

  newCfg.backend_hostport = host;
  if (caProvided) newCfg.ca_pem = caPem;
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

  String saveError;
  if (!saveConfig(newCfg, &saveError)) {
    server.send(
      500,
      "text/plain; charset=utf-8",
      String(saveError.length() > 0 ? saveError : "PAIRING-NVS-001") + " 設定未寫入，裝置不會重新啟動"
    );
    return;
  }
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
  portalApSsid = apSsid;
  portalApPassword = apPassword;

  bool apOk = WiFi.softAP(apSsid.c_str(), apPassword.c_str());
  (void)apOk;

#if DEBUG_LOG
  DBG_PRINT("[CFG] softAP result = "); DBG_PRINTLN(apOk ? "OK" : "FAIL");
  DBG_PRINT("[CFG] AP SSID = "); DBG_PRINTLN(apSsid);
  DBG_PRINT("[CFG] AP IP   = "); DBG_PRINTLN(WiFi.softAPIP());
#endif

#if INKTIME_PHOTOPAINTER_ENABLED
  if (apOk) {
    (void)photoPainter.displayPairingScreen(
      apSsid.c_str(), apPassword.c_str(), "http://192.168.4.1");
  }
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
  return backendTransportAllowed(base, cfg.ca_pem);
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
    if (pending.eventEpoch > 0) payload["event_epoch"] = pending.eventEpoch;
  }
  if (pending.errorCode.length() > 0U) payload["error_code"] = pending.errorCode;
  String body;
  serializeJson(payload, body);

  for (uint8_t attempt = 0; attempt <= inktime::kQueueRetryLimit; ++attempt) {
    inktime::DeviceHttpTransport transport(cfg.ca_pem);
    HTTPClient ackHttp;
    String transportCode;
    String transportMessage;
    if (!transport.begin(ackHttp, base + String(DEVICE_QUEUE_ACK_PATH), 15000, transportCode, transportMessage)) {
      lastDeviceErrorCode = transportCode;
      lastDeviceErrorMessage = transportMessage;
      break;
    }
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
  const time_t eventNow = time(nullptr);
  PendingQueueAck pending = {
    currentQueueItemId,
    static_cast<int32_t>(currentQueueVersion),
    event,
    displaySkipped,
    errorCode.substring(0, 64),
    delayedTerminal,
    currentReleaseId,
    delayedTerminal && eventNow > 0 ? static_cast<int64_t>(eventNow) : 0,
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

  inktime::DeviceHttpTransport transport(cfg.ca_pem);
  HTTPClient manifestHttp;
  String transportCode;
  String transportMessage;
  const char* manifestHeaders[] = {"Content-Type"};
  manifestHttp.collectHeaders(manifestHeaders, 1);
  if (!transport.begin(manifestHttp, manifestUrl, 30000, transportCode, transportMessage)) {
    lastDeviceErrorCode = transportCode.length() ? transportCode : "DEVICE-MANIFEST-URL";
    lastDeviceErrorMessage = transportMessage.length() ? transportMessage : "Manifest URL 無法初始化";
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
#if INKTIME_PHOTOPAINTER_ENABLED
    bool validSchedule = applyRemoteSchedule(
      remoteConfig,
      remoteConfig["schema_version"] | 0,
      candidate
    );
#else
    const bool validSchedule = true;
#endif
    bool validRemote = offsetMinutes >= -12 * 60 && offsetMinutes <= 14 * 60
      && remoteHour >= 0 && remoteHour <= 23 && remoteMinute >= 0 && remoteMinute <= 59
      && (rotation == 0 || rotation == 180) && compatiblePanel
      && desiredConfigVersion >= cfg.config_version && validSchedule;
    if (!validRemote) {
      lastDeviceErrorCode = "DEVICE-CONFIG-PROFILE";
      lastDeviceErrorMessage = "遠端設定版本或面板 Profile 與韌體不相容";
      return false;
    }
#if INKTIME_PHOTOPAINTER_ENABLED
    bool scheduleChanged = cfg.schedule_count != candidate.schedule_count;
    if (!scheduleChanged) {
      for (uint8_t index = 0; index < candidate.schedule_count; ++index) {
        if (cfg.schedule_slots[index].hour != candidate.schedule_slots[index].hour
            || cfg.schedule_slots[index].minute != candidate.schedule_slots[index].minute) {
          scheduleChanged = true;
          break;
        }
      }
    }
#endif
    if (
        cfg.tz_offset_minutes != offsetMinutes || cfg.refresh_hour != remoteHour
        || cfg.refresh_minute != remoteMinute || cfg.rotate180 != (rotation == 180)
        || cfg.config_version != desiredConfigVersion
#if INKTIME_PHOTOPAINTER_ENABLED
        || scheduleChanged
        || cfg.delivery_mode != candidate.delivery_mode
        || cfg.schedule_count != candidate.schedule_count
        || cfg.prefetch_lead_minutes != candidate.prefetch_lead_minutes
        || cfg.button_wake_action != candidate.button_wake_action) {
#else
        ) {
#endif
      candidate.tz_offset_minutes = offsetMinutes;
      candidate.refresh_hour = static_cast<uint8_t>(remoteHour);
      candidate.refresh_minute = static_cast<uint8_t>(remoteMinute);
      candidate.rotate180 = rotation == 180;
      candidate.config_version = desiredConfigVersion;
      String persistError;
      if (!saveConfig(candidate, &persistError)) {
        setConfigPersistenceError(persistError);
        return false;
      }
      cfg = candidate;
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
    inktime::DeviceHttpTransport fileTransport(cfg.ca_pem);
    HTTPClient fileHttp;
    String fileTransportCode;
    String fileTransportMessage;
    const char* fileHeaders[] = {"Content-Type"};
    fileHttp.collectHeaders(fileHeaders, 1);
    if (!fileTransport.begin(fileHttp, fileUrl, 60000, fileTransportCode, fileTransportMessage)) {
      lastDeviceErrorCode = fileTransportCode;
      lastDeviceErrorMessage = fileTransportMessage;
      continue;
    }
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
  inktime::DisplayRotation rotation,
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
  inktime::DeviceHttpTransport fileTransport(cfg.ca_pem);
  HTTPClient fileHttp;
  String fileTransportCode;
  String fileTransportMessage;
  const char* fileHeaders[] = {"Content-Type"};
  fileHttp.collectHeaders(fileHeaders, 1);
  if (!fileTransport.begin(fileHttp, base + downloadUrl, 60000, fileTransportCode, fileTransportMessage)) {
    heap_caps_free(packed);
    lastDeviceErrorCode = fileTransportCode.length() ? fileTransportCode : "DEVICE-OFFLINE-SCHEDULE-URL";
    lastDeviceErrorMessage = fileTransportMessage.length() ? fileTransportMessage : "離線排程 Slot URL 無法初始化";
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

static bool failOfflineScheduleTransaction(const String &message) {
  offlineScheduleTxnBlocked = true;
  lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-TXN";
  lastDeviceErrorMessage = message;
  return false;
}

static bool commitActiveScheduleAndConfig(
  Config &cfg,
  const Config &candidate,
  const String &scheduleId,
  const String &scheduleJson,
  String &errorOut
) {
  errorOut = "";
  inktime::DeviceConfigStore::Prepared prepared;
  String persistError;
  if (!configStore.prepare(configPayload(candidate), prepared, persistError)) {
    errorOut = persistError;
    setConfigPersistenceError(persistError);
    return false;
  }
  inktime::configstore::RecoveryJournal journal;
  journal.phase = inktime::configstore::JournalPhase::Prepared;
  journal.target_schedule_id = scheduleId.c_str();
  journal.previous_active_slot = prepared.previous_active_slot;
  journal.previous_generation = prepared.previous_generation;
  journal.prepared_slot = prepared.prepared_slot;
  journal.prepared_generation = prepared.prepared_generation;
  if (!configStore.writeJournal(journal, persistError)) {
    errorOut = persistError;
    setConfigPersistenceError(persistError);
    return false;
  }
  if (!photoPainter.writeActiveSchedule(scheduleJson.c_str(), scheduleJson.length())
      || photoPainter.activeScheduleId() != scheduleId) {
    return failOfflineScheduleTransaction("離線排程 active schedule 寫入或身分驗證失敗");
  }
  journal.phase = inktime::configstore::JournalPhase::SchedulePromoted;
  if (!configStore.writeJournal(journal, persistError)) {
    errorOut = persistError;
    return failOfflineScheduleTransaction("離線排程 promotion journal 無法寫入");
  }
  if (!configStore.commit(prepared, persistError)) {
    errorOut = persistError;
    return failOfflineScheduleTransaction("離線排程 Config A/B pointer 無法 commit");
  }
  journal.phase = inktime::configstore::JournalPhase::ConfigCommitted;
  if (!configStore.writeJournal(journal, persistError)
      || !configStore.clearJournal(persistError)) {
    errorOut = persistError;
    return failOfflineScheduleTransaction("離線排程完成 journal 無法清除");
  }
  cfg = candidate;
  serverConfigChanged = true;
  offlineScheduleTxnBlocked = false;
  return true;
}

static bool downloadOfflineScheduleAndFrames(Config &cfg, bool targetNext = false) {
  if (offlineScheduleTxnBlocked) {
    return failOfflineScheduleTransaction("離線排程 transaction 尚未完成 recovery");
  }
  String base;
  if (cfg.backend_hostport.length() == 0 || cfg.device_token.length() == 0
      || !normalizedBackendBase(cfg, base)) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-CONFIG";
    lastDeviceErrorMessage = "離線排程缺少 Backend 或裝置 Token";
    return false;
  }
  inktime::DeviceHttpTransport scheduleTransport(cfg.ca_pem);
  HTTPClient scheduleHttp;
  String scheduleTransportCode;
  String scheduleTransportMessage;
  const char* scheduleHeaders[] = {"Content-Type"};
  scheduleHttp.collectHeaders(scheduleHeaders, 1);
  String schedulePath = DEVICE_OFFLINE_SCHEDULE_PATH;
  if (targetNext) schedulePath += "?target=next";
  if (!scheduleTransport.begin(
        scheduleHttp, base + schedulePath, 30000, scheduleTransportCode, scheduleTransportMessage)) {
    lastDeviceErrorCode = scheduleTransportCode.length() ? scheduleTransportCode : "DEVICE-OFFLINE-SCHEDULE-URL";
    lastDeviceErrorMessage = scheduleTransportMessage.length() ? scheduleTransportMessage : "離線排程 URL 無法初始化";
    return false;
  }
  scheduleHttp.addHeader("Authorization", "Bearer " + cfg.device_token);
  const int status = scheduleHttp.GET();
  const int length = scheduleHttp.getSize();
  const String contentType = scheduleHttp.header("Content-Type");
  if (status == HTTP_CODE_NOT_FOUND && length > 0 && length <= 4096
      && contentType.startsWith("application/json")) {
    JsonDocument notReady;
    const DeserializationError notReadyError = deserializeJson(notReady, scheduleHttp.getStream());
    scheduleHttp.end();
    const String errorName = notReady["error"] | "";
    const String notReadyTarget = notReady["target"] | "";
    const JsonVariantConst rawRetryEpoch = notReady["retry_after_epoch"];
    const JsonVariantConst rawNextSlotEpoch = notReady["next_slot_epoch"];
    int64_t serverRetryEpoch = 0;
    int64_t nextSlotEpoch = 0;
    bool nextSlotFieldValid = rawNextSlotEpoch.isNull();
    if (!notReadyError && !notReady.overflowed()
        && errorName == "schedule_not_ready"
        && ((targetNext && notReadyTarget == "next")
            || (!targetNext && (notReadyTarget.length() == 0U || notReadyTarget == "current")))
        && rawRetryEpoch.is<int64_t>() && !rawRetryEpoch.is<bool>()) {
      serverRetryEpoch = rawRetryEpoch.as<int64_t>();
    }
    if (!notReadyError && !notReady.overflowed()
        && errorName == "schedule_not_ready"
        && rawNextSlotEpoch.is<int64_t>() && !rawNextSlotEpoch.is<bool>()
        && rawNextSlotEpoch.as<int64_t>() > 0) {
      nextSlotEpoch = rawNextSlotEpoch.as<int64_t>();
      nextSlotFieldValid = true;
    }
    if (!nextSlotFieldValid) {
      serverRetryEpoch = 0;
      nextSlotEpoch = 0;
    }
    time_t recoveryNow = time(nullptr);
    if (recoveryNow <= 0) (void)photoPainter.readRtc(recoveryNow);
    if (errorName == "schedule_not_ready"
        && ((targetNext && notReadyTarget == "next")
            || (!targetNext && (notReadyTarget.length() == 0U || notReadyTarget == "current")))) {
      const time_t retryEpoch = scheduleOfflineRecovery(
        recoveryNow, serverRetryEpoch, nextSlotEpoch);
      lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-NOT-READY";
      lastDeviceErrorMessage = retryEpoch > recoveryNow
        ? "伺服器尚未準備今日離線排程；已保存 bounded retry epoch"
        : "伺服器尚未準備今日離線排程；時間無效，使用 bounded fallback";
      return false;
    }
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-HTTP";
    lastDeviceErrorMessage = "離線排程 schedule_not_ready JSON 不合法";
    return false;
  }
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
  const String responseTarget = schedule["target"] | "current";
  const String deliveryMode = schedule["delivery_mode"] | "";
  const String targetLocalDate = schedule["target_local_date"] | "";
  const String targetDate = schedule["target_date"] | "";
  const String timezoneName = schedule["timezone"] | "";
  const String scheduleId = schedule["schedule_id"] | "";
  const String panelProfile = schedule["panel_profile"] | "";
  const String buttonWakeAction = schedule["button_wake_action"] | "";
  const JsonVariantConst rawConfigVersion = schedule["config_version"];
  const JsonVariantConst rawRotation = schedule["rotation"];
  const JsonVariantConst rawScheduleVersion = schedule["offline_schedule_version"];
  const JsonVariantConst rawTargetStartEpoch = schedule["target_start_epoch"];
  const JsonVariantConst rawTargetEndEpoch = schedule["target_end_epoch"];
  if (jsonError || schedule.overflowed() || responseTarget != (targetNext ? "next" : "current")
      || !schema.is<int32_t>() || schema.is<bool>()
      || schema.as<int32_t>() != 1 || deliveryMode != "inktime_offline_schedule"
      || !rawSlots.is<JsonArrayConst>() || targetLocalDate.length() == 0U
      || (targetDate.length() > 0U && targetDate != targetLocalDate)
      || timezoneName.length() == 0U || timezoneName.length() > 64U
      || scheduleId.length() == 0U
      || !inktime::boundedText(scheduleId.c_str(), inktime::kQueueIdentifierMaxBytes)
      || panelProfile.length() == 0U || !buttonWakeAction.length()
      || !rawConfigVersion.is<uint32_t>() || rawConfigVersion.is<bool>()
      || !rawRotation.is<int32_t>() || rawRotation.is<bool>()
      || !rawScheduleVersion.is<int32_t>() || rawScheduleVersion.is<bool>()
      || rawScheduleVersion.as<int32_t>() < 0
      || !rawTargetStartEpoch.is<int64_t>() || rawTargetStartEpoch.is<bool>()
      || !rawTargetEndEpoch.is<int64_t>() || rawTargetEndEpoch.is<bool>()
      || rawTargetStartEpoch.as<int64_t>() <= 0
      || rawTargetEndEpoch.as<int64_t>() <= rawTargetStartEpoch.as<int64_t>()) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-SCHEMA";
    lastDeviceErrorMessage = "離線排程 schema、日期或快照欄位不相容";
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
      || rawTimes.size() > inktime::kMaxOfflineSlots || rawTimes.size() != slots.size()) {
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
  time_t rtcEpoch = 0;
  if (!photoPainter.readRtc(rtcEpoch)) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-RTC";
    lastDeviceErrorMessage = "離線排程無法由 PhotoPainter RTC 驗證本地日期";
    return false;
  }
  const int64_t targetStartEpoch = rawTargetStartEpoch.as<int64_t>();
  const int64_t targetEndEpoch = rawTargetEndEpoch.as<int64_t>();
  int64_t activeTargetEndEpoch = 0;
  bool targetDateIsNext = false;
  String activeTargetDate;
  if (targetNext) {
    String activeJson;
    JsonDocument active;
    const bool activeReadable = photoPainter.readActiveSchedule(activeJson)
      && !deserializeJson(active, activeJson)
      && !active.overflowed();
    const JsonVariantConst activeEnd = active["target_end_epoch"];
    activeTargetDate = active["target_local_date"] | "";
    if (activeTargetDate.length() == 0U) activeTargetDate = active["target_date"] | "";
    String expectedNextDate;
    if (activeReadable && activeEnd.is<int64_t>() && !activeEnd.is<bool>()
        && activeEnd.as<int64_t>() > 0
        && nextIsoLocalDate(activeTargetDate, expectedNextDate)
        && targetLocalDate == expectedNextDate
        && targetStartEpoch == activeEnd.as<int64_t>()) {
      activeTargetEndEpoch = activeEnd.as<int64_t>();
      targetDateIsNext = true;
    }
    if (!targetDateIsNext || static_cast<int64_t>(rtcEpoch) >= targetStartEpoch) {
      lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-FUTURE";
      lastDeviceErrorMessage = "離線排程 next 快照不是目前 active 的下一個本地日";
      return false;
    }
  } else if (static_cast<int64_t>(rtcEpoch) < targetStartEpoch
             || static_cast<int64_t>(rtcEpoch) >= targetEndEpoch) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-RTC";
    lastDeviceErrorMessage = "離線排程 RTC 不在伺服器授權的目標本地日範圍";
    return false;
  }
  const uint32_t remoteConfigVersion = rawConfigVersion.as<uint32_t>();
  const int32_t remoteRotation = rawRotation.as<int32_t>();
  inktime::OfflineScheduleContract contract = {
    1,
    deliveryMode.c_str(),
    targetLocalDate.c_str(),
    targetLocalDate.c_str(),
    timezoneName.c_str(),
    targetStartEpoch,
    targetEndEpoch,
    static_cast<int64_t>(rtcEpoch),
    remoteConfigVersion,
    cfg.config_version,
    remoteRotation,
    panelProfile.c_str(),
    INKTIME_PANEL_PROFILE,
    buttonWakeAction.c_str(),
    static_cast<uint8_t>(slots.size()),
    static_cast<uint8_t>(rawTimes.size()),
    true,
    true,
    true,
  };
  inktime::OfflineNextScheduleContract nextContract = {
    1,
    deliveryMode.c_str(),
    targetLocalDate.c_str(),
    timezoneName.c_str(),
    targetStartEpoch,
    targetEndEpoch,
    activeTargetEndEpoch,
    static_cast<int64_t>(rtcEpoch),
    remoteConfigVersion,
    cfg.config_version,
    remoteRotation,
    panelProfile.c_str(),
    INKTIME_PANEL_PROFILE,
    buttonWakeAction.c_str(),
    static_cast<uint8_t>(slots.size()),
    static_cast<uint8_t>(rawTimes.size()),
    targetDateIsNext,
    true,
    true,
    true,
  };
  int64_t previousShowAtEpoch = 0;
  for (size_t index = 0; index < slots.size(); ++index) {
    const JsonVariantConst rawSlot = slots[index];
    if (!rawSlot.is<JsonObjectConst>()) {
      contract.queueIdentityValid = false;
      contract.sha256Valid = false;
      break;
    }
    const JsonObjectConst slot = rawSlot.as<JsonObjectConst>();
    const JsonVariantConst rawSize = slot["size"];
    const JsonVariantConst rawWidth = slot["width"];
    const JsonVariantConst rawHeight = slot["height"];
    const JsonVariantConst rawSlotIndex = slot["slot_index"];
    const JsonVariantConst rawQueueVersion = slot["queue_version"];
    const JsonVariantConst rawShowAtEpoch = slot["show_at_epoch"];
    const String itemId = slot["queue_item_id"] | "";
    const String releaseId = slot["release_id"] | "";
    const String slotScheduleId = slot["offline_schedule_id"] | "";
    const String sha = slot["sha256"] | "";
    const String downloadUrl = slot["download_url"] | "";
    const String pixelFormat = slot["pixel_format"] | "";
    const String renderProfile = slot["render_profile"] | "";
    const bool indexed4 = pixelFormat == "indexed4";
    const int64_t expectedSize = static_cast<int64_t>(FB_WIDTH) * FB_HEIGHT
      / (indexed4 ? 2 : 4);
    const bool validIdentity = rawSlot.is<JsonObjectConst>()
      && rawSlotIndex.is<int32_t>() && !rawSlotIndex.is<bool>()
      && rawSlotIndex.as<int32_t>() == static_cast<int32_t>(index)
      && rawQueueVersion.is<int32_t>() && !rawQueueVersion.is<bool>()
      && rawQueueVersion.as<int32_t>() >= 0
      && inktime::boundedText(itemId.c_str(), inktime::kQueueIdentifierMaxBytes)
      && inktime::boundedText(releaseId.c_str(), inktime::kQueueIdentifierMaxBytes)
      && inktime::boundedText(slotScheduleId.c_str(), inktime::kQueueIdentifierMaxBytes)
      && slotScheduleId == scheduleId
      && inktime::isSafeQueueDownloadPathForItem(
           downloadUrl.c_str(), downloadUrl.length(), itemId.c_str())
      && (renderProfile == "safe_4c" || renderProfile == String(INKTIME_PANEL_PROFILE));
    const bool validPayload = rawSize.is<int64_t>() && !rawSize.is<bool>()
      && rawSize.as<int64_t>() == expectedSize
      && rawWidth.is<int32_t>() && !rawWidth.is<bool>()
      && rawHeight.is<int32_t>() && !rawHeight.is<bool>()
      && rawWidth.as<int32_t>() == FB_WIDTH && rawHeight.as<int32_t>() == FB_HEIGHT
      && (pixelFormat == "indexed4" || pixelFormat == "2bpp")
      && inktime::isSha256Hex(sha.c_str());
    const bool validShowAtEpoch = rawShowAtEpoch.is<int64_t>() && !rawShowAtEpoch.is<bool>()
      && rawShowAtEpoch.as<int64_t>() >= targetStartEpoch
      && rawShowAtEpoch.as<int64_t>() < targetEndEpoch
      && (index == 0U || rawShowAtEpoch.as<int64_t>() > previousShowAtEpoch);
    contract.queueIdentityValid = contract.queueIdentityValid && validIdentity;
    contract.sha256Valid = contract.sha256Valid && validPayload;
    contract.slotEpochsValid = contract.slotEpochsValid && validShowAtEpoch;
    nextContract.queueIdentityValid = nextContract.queueIdentityValid && validIdentity;
    nextContract.sha256Valid = nextContract.sha256Valid && validPayload;
    nextContract.slotEpochsValid = nextContract.slotEpochsValid && validShowAtEpoch;
    if (validShowAtEpoch) previousShowAtEpoch = rawShowAtEpoch.as<int64_t>();
  }
  const bool validContract = targetNext
    ? inktime::validOfflineNextScheduleContract(nextContract)
    : inktime::validOfflineScheduleContract(contract);
  if (!validContract) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-SCHEMA";
    lastDeviceErrorMessage = "離線排程快照過期、日期錯誤或 Slot 完整性不合法";
    return false;
  }
  scheduleCandidate.schedule_count = static_cast<uint8_t>(rawTimes.size());
  for (uint8_t index = 0; index < scheduleCandidate.schedule_count; ++index) {
    scheduleCandidate.schedule_slots[index] = scheduleSlots[index];
  }
  scheduleCandidate.refresh_hour = scheduleSlots[0].hour;
  scheduleCandidate.refresh_minute = scheduleSlots[0].minute;
  scheduleCandidate.rotate180 = remoteRotation == 180;
  scheduleCandidate.delivery_mode = deliveryMode;
  scheduleCandidate.button_wake_action = buttonWakeAction;
  scheduleCandidate.config_version = remoteConfigVersion;
  const JsonVariantConst rawLead = schedule["prefetch_lead_minutes"];
  if (!rawLead.is<int32_t>() || rawLead.is<bool>()) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-SCHEMA";
    lastDeviceErrorMessage = "離線排程 prefetch_lead_minutes 缺少或型別不合法";
    return false;
  }
  const int remoteLead = rawLead.as<int32_t>();
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
    const JsonVariantConst rawShowAtEpoch = slot["show_at_epoch"];
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
        || !rawShowAtEpoch.is<int64_t>() || rawShowAtEpoch.is<bool>()
        || rawShowAtEpoch.as<int64_t>() < targetStartEpoch
        || rawShowAtEpoch.as<int64_t>() >= targetEndEpoch
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
             cfg,
             remoteRotation == 180
               ? inktime::DisplayRotation::Rotate180
               : inktime::DisplayRotation::Rotate0,
             base,
             downloadUrl,
             sha,
             pixelFormat,
             rawSize.as<int64_t>())
        || !sendQueueEvent(cfg, inktime::QueueEvent::DownloadCompleted)) {
      return false;
    }
    currentPayloadShaVerified = true;
    if (!sendQueueEvent(cfg, inktime::QueueEvent::HashVerified)) return false;
  }
  String scheduleJson;
  serializeJson(schedule, scheduleJson);
  if (targetNext) {
    const bool stored = photoPainter.writeStagedNextSchedule(
      scheduleJson.c_str(), scheduleJson.length());
    if (!stored || photoPainter.stagedNextScheduleId() != scheduleId) {
      lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-STAGE";
      lastDeviceErrorMessage = "離線排程 staged next 無法原子寫入或身分驗證失敗";
      return false;
    }
    // The future snapshot remains a staged artifact.  Its rotation, schedule,
    // lead and config version are not applied until the target local midnight.
    currentFromQueue = false;
    currentPayloadIntegrityTrusted = true;
    return true;
  }
  String persistError;
  if (!commitActiveScheduleAndConfig(
        cfg, scheduleCandidate, scheduleId, scheduleJson, persistError)) {
    if (persistError.length() > 0U) setConfigPersistenceError(persistError);
    return false;
  }
  clearOfflineRetryState();
  currentFromQueue = false;
  currentPayloadIntegrityTrusted = true;
  return true;
}

static bool reconcilePendingScheduleConfigTransaction(Config &cfg) {
  inktime::configstore::RecoveryJournal journal;
  bool present = false;
  String journalError;
  if (!configStore.readJournal(journal, present, journalError)) {
    return failOfflineScheduleTransaction("離線排程 recovery journal 損壞，已 fail-closed");
  }
  if (!present) return true;

  inktime::configstore::ConfigPayload preparedPayload;
  String preparedError;
  if (!configStore.readPrepared(
        journal.prepared_slot, journal.prepared_generation, preparedPayload, preparedError)) {
    return failOfflineScheduleTransaction("離線排程 recovery 的 prepared Config 不存在或損壞");
  }
  inktime::configstore::ConfigPayload activePayload;
  char activeSlot = 0;
  uint64_t activeGeneration = 0U;
  String activeError;
  const bool activePresent = configStore.readActive(
    activePayload, activeSlot, activeGeneration, activeError);
  const String activeScheduleId = photoPainter.activeScheduleId();
  const bool previousPointer = journal.previous_active_slot != 0
    && activePresent
    && activeSlot == journal.previous_active_slot
    && activeGeneration == journal.previous_generation;
  const bool preparedPointer = activePresent
    && activeSlot == journal.prepared_slot
    && activeGeneration == journal.prepared_generation;
  const bool targetScheduleActive = activeScheduleId == journal.target_schedule_id.c_str();

  if (journal.phase == inktime::configstore::JournalPhase::Prepared
      && !targetScheduleActive
      && (previousPointer || (!activePresent && journal.previous_active_slot == 0))) {
    String clearError;
    if (!configStore.clearJournal(clearError)) {
      return failOfflineScheduleTransaction("離線排程 stale prepared journal 無法清除");
    }
    return true;
  }
  if (activeScheduleId.length() == 0U || !targetScheduleActive) {
    return failOfflineScheduleTransaction("離線排程 active 身分與 recovery target 不一致");
  }
  if (!activePresent) {
    return failOfflineScheduleTransaction("離線排程 active schedule 存在但 Config pointer 遺失");
  }
  if (!preparedPointer) {
    String commitError;
    if (!configStore.commitPreparedSlot(
          journal.prepared_slot,
          journal.prepared_generation,
          preparedPayload,
          commitError)) {
      return failOfflineScheduleTransaction("離線排程 recovery 無法 commit prepared Config");
    }
  }
  journal.phase = inktime::configstore::JournalPhase::ConfigCommitted;
  String commitJournalError;
  if (!configStore.writeJournal(journal, commitJournalError)
      || !configStore.clearJournal(commitJournalError)) {
    return failOfflineScheduleTransaction("離線排程 recovery journal 無法完成清除");
  }
  applyConfigPayload(preparedPayload, cfg);
  serverConfigChanged = true;
  offlineScheduleTxnBlocked = false;
  return true;
}
#endif

static QueueDownloadResult downloadQueuePhotoBin(Config &cfg) {
#if INKTIME_PHOTOPAINTER_ENABLED
  if (cfg.delivery_mode == "inktime_offline_schedule") {
    // Enhanced offline schedules are consumed only from the signed schedule
    // endpoint; the generic queue can never select an arbitrary first item.
    return QueueDownloadResult::EmptyOrUnsupported;
  }
#endif
  String base;
  if (!normalizedBackendBase(cfg, base)) return QueueDownloadResult::Failed;

  inktime::DeviceHttpTransport manifestTransport(cfg.ca_pem);
  HTTPClient manifestHttp;
  String manifestTransportCode;
  String manifestTransportMessage;
  const char* manifestHeaders[] = {"Content-Type"};
  manifestHttp.collectHeaders(manifestHeaders, 1);
  if (!manifestTransport.begin(
        manifestHttp, base + String(DEVICE_QUEUE_MANIFEST_PATH), 30000,
        manifestTransportCode, manifestTransportMessage)) {
    lastDeviceErrorCode = manifestTransportCode.length() ? manifestTransportCode : "DEVICE-QUEUE-URL";
    lastDeviceErrorMessage = manifestTransportMessage.length() ? manifestTransportMessage : "Queue Manifest URL 無法初始化";
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
#if INKTIME_PHOTOPAINTER_ENABLED
  bool selected = false;
  const bool enhancedOffline = cfg.delivery_mode == "inktime_offline_schedule";
#endif
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
#if INKTIME_PHOTOPAINTER_ENABLED
    const JsonVariantConst rawOfflinePrefetch = item["offline_prefetch_allowed"];
    if (!rawOfflinePrefetch.isNull()
        && (!rawOfflinePrefetch.is<bool>() || rawOfflinePrefetch.is<int>())) {
      lastDeviceErrorCode = "DEVICE-QUEUE-ITEM";
      lastDeviceErrorMessage = "offline_prefetch_allowed 必須是真正 JSON boolean";
      return QueueDownloadResult::Failed;
    }
    const bool offlinePrefetchAllowed = rawOfflinePrefetch | false;
    const String deliveryMode = item["delivery_mode"] | "online_queue";
#endif
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
#if INKTIME_PHOTOPAINTER_ENABLED
    if (enhancedOffline && (deliveryMode != "offline_schedule" || !offlinePrefetchAllowed)) {
      continue;
    }
    if (!selected) {
#else
    if (index == 0U) {
#endif
      selectedItemId = itemId;
      selectedReleaseId = releaseId;
      selectedSha = sha;
      selectedDownloadUrl = downloadUrl;
      selectedPixelFormat = pixelFormat;
      selectedRenderProfile = renderProfile;
      selectedSize = size;
#if INKTIME_PHOTOPAINTER_ENABLED
      selected = true;
#endif
    }
  }

#if INKTIME_PHOTOPAINTER_ENABLED
  if (!selected) return QueueDownloadResult::EmptyOrUnsupported;
#endif

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

  inktime::DeviceHttpTransport fileTransport(cfg.ca_pem);
  HTTPClient fileHttp;
  String fileTransportCode;
  String fileTransportMessage;
  const char* fileHeaders[] = {"Content-Type"};
  fileHttp.collectHeaders(fileHeaders, 1);
  if (!fileTransport.begin(
        fileHttp, base + selectedDownloadUrl, 60000, fileTransportCode, fileTransportMessage)) {
    heap_caps_free(packed);
    lastDeviceErrorCode = fileTransportCode.length() ? fileTransportCode : "DEVICE-QUEUE-FILE-URL";
    lastDeviceErrorMessage = fileTransportMessage.length() ? fileTransportMessage : "Queue download URL 無法初始化";
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

#if INKTIME_PHOTOPAINTER_ENABLED
static bool activeOfflineEpochs(
  time_t nowEpoch,
  time_t &nextDisplayEpoch,
  time_t &targetEndEpoch,
  time_t &nextSchedulePrefetchEpoch
) {
  nextDisplayEpoch = 0;
  targetEndEpoch = 0;
  nextSchedulePrefetchEpoch = 0;
  String activeJson;
  if (!photoPainter.readActiveSchedule(activeJson)) return false;
  JsonDocument active;
  if (deserializeJson(active, activeJson) || active.overflowed()) return false;
  const JsonVariantConst rawEnd = active["target_end_epoch"];
  const JsonVariantConst rawPrefetch = active["next_schedule_prefetch_epoch"];
  const JsonVariantConst rawSlots = active["slots"];
  if (!rawEnd.is<int64_t>() || rawEnd.is<bool>() || rawEnd.as<int64_t>() <= 0
      || !rawSlots.is<JsonArrayConst>()) return false;
  if (!rawPrefetch.isNull()
      && (!rawPrefetch.is<int64_t>() || rawPrefetch.is<bool>() || rawPrefetch.as<int64_t>() < 0)) {
    return false;
  }
  targetEndEpoch = static_cast<time_t>(rawEnd.as<int64_t>());
  if (!rawPrefetch.isNull()) {
    nextSchedulePrefetchEpoch = static_cast<time_t>(rawPrefetch.as<int64_t>());
  }
  const JsonArrayConst slots = rawSlots.as<JsonArrayConst>();
  for (size_t index = 0; index < slots.size(); ++index) {
    const JsonVariantConst rawSlot = slots[index];
    if (!rawSlot.is<JsonObjectConst>()) return false;
    const JsonVariantConst rawShowAt = rawSlot["show_at_epoch"];
    if (!rawShowAt.is<int64_t>() || rawShowAt.is<bool>() || rawShowAt.as<int64_t>() <= 0) {
      return false;
    }
    const time_t showAt = static_cast<time_t>(rawShowAt.as<int64_t>());
    if (showAt > nowEpoch && (nextDisplayEpoch == 0 || showAt < nextDisplayEpoch)) {
      nextDisplayEpoch = showAt;
    }
  }
  return true;
}

static bool activeHasDueFormalSlot(time_t nowEpoch) {
  if (nowEpoch <= 0) return false;
  String activeJson;
  if (!photoPainter.readActiveSchedule(activeJson)) return false;
  JsonDocument active;
  if (deserializeJson(active, activeJson) || active.overflowed()) return false;
  const JsonArrayConst slots = active["slots"].as<JsonArrayConst>();
  if (slots.isNull() || slots.size() == 0U || slots.size() > inktime::kMaxOfflineSlots) return false;
  int64_t showAtEpochs[inktime::kMaxOfflineSlots] = {};
  for (size_t index = 0; index < slots.size(); ++index) {
    const JsonVariantConst rawSlot = slots[index];
    if (!rawSlot.is<JsonObjectConst>()) {
      return false;
    }
    const JsonVariantConst rawShowAt = rawSlot["show_at_epoch"];
    if (!rawShowAt.is<int64_t>() || rawShowAt.is<bool>()) return false;
    showAtEpochs[index] = rawShowAt.as<int64_t>();
  }
  return inktime::scheduleHasDueFormalSlot(
    showAtEpochs, static_cast<uint8_t>(slots.size()), static_cast<uint64_t>(nowEpoch));
}

static bool offlinePrefetchWake(const Config &cfg, time_t nowEpoch) {
  if (cfg.delivery_mode != "inktime_offline_schedule" || nowEpoch <= 0) {
    return false;
  }
  time_t nextDisplay = 0;
  time_t targetEnd = 0;
  time_t nextSchedulePrefetch = 0;
  if (!activeOfflineEpochs(nowEpoch, nextDisplay, targetEnd, nextSchedulePrefetch)) return true;
  if (targetEnd > 0 && nowEpoch >= targetEnd) return true;
  if (nextSchedulePrefetch > 0 && nowEpoch >= nextSchedulePrefetch
      && (targetEnd <= 0 || nowEpoch < targetEnd)) return true;
  if (cfg.prefetch_lead_minutes == 0) return false;
  if (nextDisplay <= nowEpoch) return false;
  const time_t lead = static_cast<time_t>(cfg.prefetch_lead_minutes) * 60;
  return nowEpoch >= nextDisplay - lead && nowEpoch < nextDisplay;
}

static bool offlineNextSchedulePrefetchDue(const Config &cfg, time_t nowEpoch) {
  if (cfg.delivery_mode != "inktime_offline_schedule" || nowEpoch <= 0) return false;
  time_t nextDisplay = 0;
  time_t targetEnd = 0;
  time_t nextSchedulePrefetch = 0;
  if (!activeOfflineEpochs(nowEpoch, nextDisplay, targetEnd, nextSchedulePrefetch)) return false;
  return nextSchedulePrefetch > 0 && nowEpoch >= nextSchedulePrefetch
      && targetEnd > nowEpoch;
}

static bool promoteStagedNextIfDue(Config &cfg, time_t nowEpoch) {
  if (offlineScheduleTxnBlocked) {
    return failOfflineScheduleTransaction("離線排程 transaction 尚未完成 recovery");
  }
  if (nowEpoch <= 0) return false;
  String stagedJson;
  if (!photoPainter.readStagedNextSchedule(stagedJson)) return false;
  JsonDocument staged;
  if (deserializeJson(staged, stagedJson) || staged.overflowed()) return false;
  const String responseTarget = staged["target"] | "";
  const String deliveryMode = staged["delivery_mode"] | "";
  String targetDate = staged["target_local_date"] | "";
  if (targetDate.length() == 0U) targetDate = staged["target_date"] | "";
  const String legacyTargetDate = staged["target_date"] | "";
  const String scheduleId = staged["schedule_id"] | "";
  const String timezoneName = staged["timezone"] | "";
  const String panelProfile = staged["panel_profile"] | "";
  const String buttonWakeAction = staged["button_wake_action"] | "";
  const JsonVariantConst rawSchema = staged["schema_version"];
  const JsonVariantConst rawConfig = staged["config_version"];
  const JsonVariantConst rawRotation = staged["rotation"];
  const JsonVariantConst rawScheduleVersion = staged["offline_schedule_version"];
  const JsonVariantConst rawLead = staged["prefetch_lead_minutes"];
  const JsonVariantConst rawStart = staged["target_start_epoch"];
  const JsonVariantConst rawEnd = staged["target_end_epoch"];
  const JsonArrayConst rawSlots = staged["slots"].as<JsonArrayConst>();
  JsonArrayConst rawTimes = staged["schedule_times"].as<JsonArrayConst>();
  if (rawTimes.isNull()) rawTimes = staged["schedule"].as<JsonArrayConst>();

  String activeJson;
  JsonDocument active;
  if (!photoPainter.readActiveSchedule(activeJson)
      || deserializeJson(active, activeJson) || active.overflowed()) {
    return false;
  }
  String activeDate = active["target_local_date"] | "";
  if (activeDate.length() == 0U) activeDate = active["target_date"] | "";
  const JsonVariantConst activeEnd = active["target_end_epoch"];
  String expectedNextDate;
  const bool activeBoundaryValid = activeEnd.is<int64_t>() && !activeEnd.is<bool>()
    && activeEnd.as<int64_t>() > 0
    && nextIsoLocalDate(activeDate, expectedNextDate);
  const int64_t targetStartEpoch = rawStart.is<int64_t>() && !rawStart.is<bool>()
    ? rawStart.as<int64_t>() : 0;
  const int64_t targetEndEpoch = rawEnd.is<int64_t>() && !rawEnd.is<bool>()
    ? rawEnd.as<int64_t>() : 0;
  const int64_t activeTargetEndEpoch = activeBoundaryValid
    ? activeEnd.as<int64_t>() : 0;
  const uint32_t remoteConfigVersion = rawConfig.is<uint32_t>() && !rawConfig.is<bool>()
    ? rawConfig.as<uint32_t>() : 0;
  const int32_t remoteRotation = rawRotation.is<int32_t>() && !rawRotation.is<bool>()
    ? rawRotation.as<int32_t>() : -1;
  const int remoteLead = rawLead.is<int32_t>() && !rawLead.is<bool>()
    ? rawLead.as<int32_t>() : -1;
  if (responseTarget != "next" || deliveryMode != "inktime_offline_schedule"
      || !rawSchema.is<int32_t>() || rawSchema.is<bool>()
      || rawSchema.as<int32_t>() != inktime::kOfflineScheduleSchemaVersion
      || targetDate.length() != 10U
      || (legacyTargetDate.length() > 0U && legacyTargetDate != targetDate)
      || targetDate != expectedNextDate || timezoneName.length() == 0U
      || timezoneName.length() > 64U || !inktime::boundedText(scheduleId.c_str(), inktime::kQueueIdentifierMaxBytes)
      || panelProfile.length() == 0U
      || (panelProfile != "safe_4c" && panelProfile != String(INKTIME_PANEL_PROFILE))
      || !validButtonWakeAction(buttonWakeAction) || remoteLead < 0 || remoteLead > 120
      || targetStartEpoch <= 0 || targetEndEpoch <= targetStartEpoch
      || targetStartEpoch != activeTargetEndEpoch
      || remoteConfigVersion < cfg.config_version
      || (remoteRotation != 0 && remoteRotation != 180)
      || !rawScheduleVersion.is<int32_t>() || rawScheduleVersion.is<bool>()
      || rawScheduleVersion.as<int32_t>() < 0
      || rawSlots.isNull() || rawSlots.size() == 0U
      || rawSlots.size() > inktime::kMaxOfflineSlots
      || rawTimes.isNull() || rawTimes.size() != rawSlots.size()
      || !activeBoundaryValid || nowEpoch < targetStartEpoch || nowEpoch >= targetEndEpoch) {
    if (targetEndEpoch > 0 && nowEpoch >= targetEndEpoch) {
      (void)photoPainter.clearStagedNextSchedule();
    }
    return false;
  }

  inktime::OfflineSlot scheduleSlots[inktime::kMaxOfflineSlots] = {};
  for (size_t index = 0; index < rawTimes.size(); ++index) {
    if (!parseOfflineClock(rawTimes[index] | "", scheduleSlots[index])) return false;
  }
  if (!inktime::validateOfflineSlots(scheduleSlots, static_cast<uint8_t>(rawTimes.size()))) {
    return false;
  }
  inktime::OfflineNextScheduleContract contract = {
    rawSchema.as<int32_t>(),
    deliveryMode.c_str(),
    targetDate.c_str(),
    timezoneName.c_str(),
    targetStartEpoch,
    targetEndEpoch,
    activeTargetEndEpoch,
    targetStartEpoch - 1,
    remoteConfigVersion,
    cfg.config_version,
    remoteRotation,
    panelProfile.c_str(),
    INKTIME_PANEL_PROFILE,
    buttonWakeAction.c_str(),
    static_cast<uint8_t>(rawSlots.size()),
    static_cast<uint8_t>(rawTimes.size()),
    true,
    true,
    true,
    true,
  };
  int64_t previousShowAtEpoch = 0;
  for (size_t index = 0; index < rawSlots.size(); ++index) {
    const JsonVariantConst rawSlot = rawSlots[index];
    if (!rawSlot.is<JsonObjectConst>()) return false;
    const JsonObjectConst slot = rawSlot.as<JsonObjectConst>();
    const JsonVariantConst rawSize = slot["size"];
    const JsonVariantConst rawWidth = slot["width"];
    const JsonVariantConst rawHeight = slot["height"];
    const JsonVariantConst rawIndex = slot["slot_index"];
    const JsonVariantConst rawVersion = slot["queue_version"];
    const JsonVariantConst rawShowAt = slot["show_at_epoch"];
    const String itemId = slot["queue_item_id"] | "";
    const String releaseId = slot["release_id"] | "";
    const String slotScheduleId = slot["offline_schedule_id"] | "";
    const String sha = slot["sha256"] | "";
    const String downloadUrl = slot["download_url"] | "";
    const String pixelFormat = slot["pixel_format"] | "";
    const String renderProfile = slot["render_profile"] | "";
    const int64_t expectedSize = static_cast<int64_t>(FB_WIDTH) * FB_HEIGHT
      / (pixelFormat == "indexed4" ? 2 : 4);
    const bool identityValid = rawIndex.is<int32_t>() && !rawIndex.is<bool>()
      && rawIndex.as<int32_t>() == static_cast<int32_t>(index)
      && rawVersion.is<int32_t>() && !rawVersion.is<bool>() && rawVersion.as<int32_t>() >= 0
      && inktime::boundedText(itemId.c_str(), inktime::kQueueIdentifierMaxBytes)
      && inktime::boundedText(releaseId.c_str(), inktime::kQueueIdentifierMaxBytes)
      && inktime::boundedText(slotScheduleId.c_str(), inktime::kQueueIdentifierMaxBytes)
      && slotScheduleId == scheduleId
      && inktime::isSafeQueueDownloadPathForItem(
        downloadUrl.c_str(), downloadUrl.length(), itemId.c_str())
      && (renderProfile == "safe_4c" || renderProfile == String(INKTIME_PANEL_PROFILE));
    const bool payloadValid = rawSize.is<int64_t>() && !rawSize.is<bool>()
      && rawSize.as<int64_t>() == expectedSize
      && rawWidth.is<int32_t>() && !rawWidth.is<bool>() && rawWidth.as<int32_t>() == FB_WIDTH
      && rawHeight.is<int32_t>() && !rawHeight.is<bool>() && rawHeight.as<int32_t>() == FB_HEIGHT
      && (pixelFormat == "indexed4" || pixelFormat == "2bpp")
      && inktime::isSha256Hex(sha.c_str());
    const bool epochValid = rawShowAt.is<int64_t>() && !rawShowAt.is<bool>()
      && rawShowAt.as<int64_t>() >= targetStartEpoch
      && rawShowAt.as<int64_t>() < targetEndEpoch
      && (index == 0U || rawShowAt.as<int64_t>() > previousShowAtEpoch);
    contract.queueIdentityValid = contract.queueIdentityValid && identityValid;
    contract.sha256Valid = contract.sha256Valid && payloadValid;
    contract.slotEpochsValid = contract.slotEpochsValid && epochValid;
    if (epochValid) previousShowAtEpoch = rawShowAt.as<int64_t>();
  }
  if (!inktime::validOfflineNextScheduleContract(contract)) return false;
  Config candidate = cfg;
  candidate.schedule_count = static_cast<uint8_t>(rawTimes.size());
  for (uint8_t index = 0; index < candidate.schedule_count; ++index) {
    candidate.schedule_slots[index] = scheduleSlots[index];
  }
  candidate.refresh_hour = scheduleSlots[0].hour;
  candidate.refresh_minute = scheduleSlots[0].minute;
  candidate.rotate180 = remoteRotation == 180;
  candidate.prefetch_lead_minutes = static_cast<uint16_t>(remoteLead);
  candidate.delivery_mode = deliveryMode;
  candidate.button_wake_action = buttonWakeAction;
  candidate.config_version = remoteConfigVersion;
  String persistError;
  inktime::DeviceConfigStore::Prepared prepared;
  if (!configStore.prepare(configPayload(candidate), prepared, persistError)) {
    setConfigPersistenceError(persistError);
    return false;
  }
  inktime::configstore::RecoveryJournal journal;
  journal.phase = inktime::configstore::JournalPhase::Prepared;
  journal.target_schedule_id = scheduleId.c_str();
  journal.previous_active_slot = prepared.previous_active_slot;
  journal.previous_generation = prepared.previous_generation;
  journal.prepared_slot = prepared.prepared_slot;
  journal.prepared_generation = prepared.prepared_generation;
  if (!configStore.writeJournal(journal, persistError)) {
    setConfigPersistenceError(persistError);
    return false;
  }
  if (!photoPainter.promoteStagedNextSchedule()
      || photoPainter.activeScheduleId() != scheduleId) {
    return failOfflineScheduleTransaction("離線排程 staged next promote 或身分驗證失敗");
  }
  journal.phase = inktime::configstore::JournalPhase::SchedulePromoted;
  if (!configStore.writeJournal(journal, persistError)
      || !configStore.commit(prepared, persistError)) {
    return failOfflineScheduleTransaction("離線排程 midnight Config commit 失敗");
  }
  journal.phase = inktime::configstore::JournalPhase::ConfigCommitted;
  if (!configStore.writeJournal(journal, persistError)
      || !configStore.clearJournal(persistError)) {
    return failOfflineScheduleTransaction("離線排程 midnight journal 無法清除");
  }
  cfg = candidate;
  serverConfigChanged = true;
  offlineScheduleTxnBlocked = false;
  clearOfflineRetryState();
  return true;
}
#endif

#if INKTIME_PHOTOPAINTER_ENABLED
static bool loadOfflineScheduledLocalFrame(
  const Config &cfg, time_t nowEpoch, bool selectNext = false);
#endif

bool downloadDailyPhotoBin(Config &cfg) {
  currentPrefetchOnly = false;
  if (!resumePendingQueueAck(cfg)) return false;
#if INKTIME_PHOTOPAINTER_ENABLED
  const bool userButtonWake = photoPainter.wokeFromUserButton();
  const bool timerRequestedNetwork = enhancedNetworkWakeRequested;
  enhancedNetworkWakeRequested = false;
  if (offlineScheduleTxnBlocked && cfg.delivery_mode == "inktime_offline_schedule") {
    return failOfflineScheduleTransaction("離線排程 transaction 尚未完成 recovery");
  }
  if (cfg.delivery_mode == "inktime_offline_schedule") {
    if (userButtonWake && cfg.button_wake_action == "check_new") {
      // A button check may replace the active schedule, but a missing or
      // unavailable remote schedule must leave the cached formal day usable.
      (void)downloadOfflineScheduleAndFrames(cfg);
      currentPrefetchOnly = false;
      return loadOfflineScheduledLocalFrame(cfg, time(nullptr), false);
    }
    if (timerRequestedNetwork || offlinePrefetchWake(cfg, time(nullptr))) {
      const time_t wakeEpoch = time(nullptr);
      const bool formalSlotDue = activeHasDueFormalSlot(wakeEpoch);
      const bool stageTomorrow = offlineNextSchedulePrefetchDue(cfg, wakeEpoch)
        && !formalSlotDue;
      const bool displayAtThisWake = cfg.prefetch_lead_minutes == 0U || formalSlotDue;
      currentPrefetchOnly = stageTomorrow || !displayAtThisWake;
      const bool prefetched = downloadOfflineScheduleAndFrames(cfg, stageTomorrow);
      if (prefetched && displayAtThisWake && !stageTomorrow) {
        currentPrefetchOnly = false;
        return loadOfflineScheduledLocalFrame(cfg, time(nullptr), false);
      }
      return prefetched;
    }
    // Enhanced offline mode has no generic/latest-photo fallback.  Formal
    // frames are selected only from the active server-authored schedule.
    return false;
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

  inktime::DeviceHttpTransport statusTransport(cfg.ca_pem);
  HTTPClient statusHttp;
  String statusTransportCode;
  String statusTransportMessage;
  if (!statusTransport.begin(
        statusHttp, base + String(DEVICE_STATUS_PATH), 15000,
        statusTransportCode, statusTransportMessage)) {
    lastDeviceErrorCode = statusTransportCode;
    lastDeviceErrorMessage = statusTransportMessage;
    return;
  }
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
#if INKTIME_PHOTOPAINTER_ENABLED
  if (offlineScheduleTxnBlocked) {
    goDeepSleepSeconds(inktime::kOfflineRetryFirstSeconds);
    return;
  }
  if (cfg.delivery_mode == "inktime_offline_schedule") {
    if (!hasTime) {
      // Unknown time is a bounded recovery wake, never a 24-hour blind sleep.
      goDeepSleepSeconds(inktime::kOfflineRetryFirstSeconds);
      return;
    }
    time_t nowEpoch = time(nullptr);
    if (nowEpoch <= 0) {
      time_t rtcEpoch = 0;
      if (photoPainter.readRtc(rtcEpoch)) nowEpoch = rtcEpoch;
    }
    if (nowEpoch > 0) {
      time_t nextDisplay = 0;
      time_t targetEnd = 0;
      time_t nextSchedulePrefetch = 0;
      if (activeOfflineEpochs(nowEpoch, nextDisplay, targetEnd, nextSchedulePrefetch)) {
        time_t wakeEpoch = 0;
        const auto considerWake = [&wakeEpoch, nowEpoch](time_t candidate) {
          if (candidate > nowEpoch && (wakeEpoch == 0 || candidate < wakeEpoch)) {
            wakeEpoch = candidate;
          }
        };
        if (nextDisplay > nowEpoch) {
          const uint64_t leadSeconds = static_cast<uint64_t>(cfg.prefetch_lead_minutes) * 60ULL;
          const time_t prefetchEpoch = nextDisplay > static_cast<time_t>(leadSeconds)
            ? nextDisplay - static_cast<time_t>(leadSeconds)
            : nextDisplay;
          considerWake(prefetchEpoch > nowEpoch ? prefetchEpoch : nextDisplay);
        }
        // The next schedule's server/IANA-computed technical deadline is an
        // independent wake candidate and may occur before today's end.
        considerWake(nextSchedulePrefetch);
        considerWake(targetEnd);
        uint8_t retryAttempt = 0;
        int64_t retryEpoch = 0;
        int64_t retryNextSlot = 0;
        if (loadOfflineRetryState(retryAttempt, retryEpoch, retryNextSlot)
            && inktime::validOfflineRetryEpoch(
              static_cast<uint64_t>(nowEpoch), retryEpoch, retryNextSlot)) {
          considerWake(static_cast<time_t>(retryEpoch));
        }
        if (wakeEpoch > nowEpoch) {
          goDeepSleepUntilEpoch(nowEpoch, wakeEpoch);
          return;
        }
        // An exhausted day uses the same persisted bounded recovery plan as
        // a missing schedule, instead of a one-minute storm.
        const time_t recoveryEpoch = scheduleOfflineRecovery(nowEpoch);
        if (recoveryEpoch > nowEpoch) goDeepSleepUntilEpoch(nowEpoch, recoveryEpoch);
        else goDeepSleepSeconds(inktime::kOfflineRetryFirstSeconds);
        return;
      }
      const time_t recoveryEpoch = scheduleOfflineRecovery(nowEpoch);
      if (recoveryEpoch > nowEpoch) goDeepSleepUntilEpoch(nowEpoch, recoveryEpoch);
      else goDeepSleepSeconds(inktime::kOfflineRetryFirstSeconds);
      return;
    }
    goDeepSleepSeconds(inktime::kOfflineRetryFirstSeconds);
    return;
  }
#endif
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

#if INKTIME_PHOTOPAINTER_ENABLED
static bool loadOfflineScheduledLocalFrame(
  const Config &cfg, time_t nowEpoch, bool selectNext) {
  if (offlineScheduleTxnBlocked) {
    return failOfflineScheduleTransaction("離線排程 transaction 尚未完成 recovery");
  }
  // Enhanced local mode is deliberately cache-only.  It never calls Wi-Fi,
  // NTP, Manifest, or status endpoints; a missing formal frame is a safe
  // no-refresh result instead of a network fallback.
  if (nowEpoch <= 0) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-FRAME";
    lastDeviceErrorMessage = "離線排程沒有可驗證的 RTC 時間";
    return false;
  }
  currentFromQueue = false;
  currentQueueItemId = "";
  currentQueueVersion = -1;
  currentReleaseId = "";
  currentRenderProfile = "";
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
  const String activeTargetDate = active["target_local_date"] | "";
  const String activeLegacyDate = active["target_date"] | "";
  const String activeDeliveryMode = active["delivery_mode"] | "";
  const String activeTimezone = active["timezone"] | "";
  const String activeScheduleId = active["schedule_id"] | "";
  const String activePanelProfile = active["panel_profile"] | "";
  const String activeButtonWakeAction = active["button_wake_action"] | "";
  const JsonVariantConst activeSchema = active["schema_version"];
  const JsonVariantConst activeConfigVersion = active["config_version"];
  const JsonVariantConst activeRotation = active["rotation"];
  const JsonVariantConst activeScheduleVersion = active["offline_schedule_version"];
  const JsonVariantConst activeTargetStartEpoch = active["target_start_epoch"];
  const JsonVariantConst activeTargetEndEpoch = active["target_end_epoch"];
  if (jsonError || active.overflowed() || !rawSlots.is<JsonArrayConst>()
      || targetDate.length() != 10 || activeTargetDate.length() == 0U
      || (activeLegacyDate.length() > 0U && activeLegacyDate != activeTargetDate)
      || !activeSchema.is<int32_t>() || activeSchema.is<bool>()
      || activeSchema.as<int32_t>() != inktime::kOfflineScheduleSchemaVersion
      || activeDeliveryMode != "inktime_offline_schedule"
      || activeTimezone.length() == 0U || activeTimezone.length() > 64U
      || activeScheduleId.length() == 0U
      || !activeConfigVersion.is<uint32_t>() || activeConfigVersion.is<bool>()
      || !activeRotation.is<int32_t>() || activeRotation.is<bool>()
      || !activeScheduleVersion.is<int32_t>() || activeScheduleVersion.is<bool>()
      || activeScheduleVersion.as<int32_t>() < 0
      || !activeTargetStartEpoch.is<int64_t>() || activeTargetStartEpoch.is<bool>()
      || !activeTargetEndEpoch.is<int64_t>() || activeTargetEndEpoch.is<bool>()
      || activeTargetStartEpoch.as<int64_t>() <= 0
      || activeTargetEndEpoch.as<int64_t>() <= activeTargetStartEpoch.as<int64_t>()) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE";
    lastDeviceErrorMessage = "離線排程 active schedule schema 不合法";
    return false;
  }
  const int64_t targetStartEpoch = activeTargetStartEpoch.as<int64_t>();
  const int64_t targetEndEpoch = activeTargetEndEpoch.as<int64_t>();
  if (static_cast<int64_t>(nowEpoch) < targetStartEpoch
      || static_cast<int64_t>(nowEpoch) >= targetEndEpoch) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE";
    lastDeviceErrorMessage = "離線排程不是伺服器授權的目前本地日期";
    return false;
  }
  const JsonArrayConst slots = rawSlots.as<JsonArrayConst>();
  JsonArrayConst rawTimes = active["schedule_times"].as<JsonArrayConst>();
  if (rawTimes.isNull()) rawTimes = active["schedule"].as<JsonArrayConst>();
  if (rawTimes.isNull() || rawTimes.size() == 0U
      || rawTimes.size() > inktime::kMaxOfflineSlots || rawTimes.size() != slots.size()) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE";
    lastDeviceErrorMessage = "離線排程 active schedule_times 不合法";
    return false;
  }
  inktime::OfflineSlot activeScheduleSlots[inktime::kMaxOfflineSlots] = {};
  for (size_t index = 0; index < rawTimes.size(); ++index) {
    if (!parseOfflineClock(rawTimes[index] | "", activeScheduleSlots[index])) {
      lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE";
      lastDeviceErrorMessage = "離線排程 active 時刻格式不合法";
      return false;
    }
  }
  if (!inktime::validateOfflineSlots(activeScheduleSlots, static_cast<uint8_t>(rawTimes.size()))
      || rawTimes.size() != cfg.schedule_count
      || !inktime::validateOfflineSlots(cfg.schedule_slots, cfg.schedule_count)) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE";
    lastDeviceErrorMessage = "離線排程 active 時刻與本機設定不一致";
    return false;
  }
  for (uint8_t index = 0; index < cfg.schedule_count; ++index) {
    if (activeScheduleSlots[index].hour != cfg.schedule_slots[index].hour
        || activeScheduleSlots[index].minute != cfg.schedule_slots[index].minute) {
      lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE";
      lastDeviceErrorMessage = "離線排程 active 時刻與本機設定不一致";
      return false;
    }
  }
  inktime::OfflineScheduleContract contract = {
    activeSchema.as<int32_t>(),
    activeDeliveryMode.c_str(),
    activeTargetDate.c_str(),
    activeTargetDate.c_str(),
    activeTimezone.c_str(),
    targetStartEpoch,
    targetEndEpoch,
    static_cast<int64_t>(nowEpoch),
    activeConfigVersion.as<uint32_t>(),
    cfg.config_version,
    activeRotation.as<int32_t>(),
    activePanelProfile.c_str(),
    INKTIME_PANEL_PROFILE,
    activeButtonWakeAction.c_str(),
    static_cast<uint8_t>(slots.size()),
    static_cast<uint8_t>(rawTimes.size()),
    true,
    true,
    true,
  };
  if (activeConfigVersion.as<uint32_t>() != cfg.config_version
      || !inktime::validOfflineScheduleContract(contract)
      || cfg.delivery_mode != "inktime_offline_schedule"
      || cfg.rotate180 != (activeRotation.as<int32_t>() == 180)) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE";
    lastDeviceErrorMessage = "離線排程 active 快照與本機設定不一致";
    return false;
  }
  int64_t previewEpochs[inktime::kMaxOfflineSlots] = {};
  String previewShaValues[inktime::kMaxOfflineSlots];
  const char* previewShaPointers[inktime::kMaxOfflineSlots] = {};
  const int16_t previewCursor = loadPreviewCursorForSchedule(activeScheduleId);
  int64_t previousShowAtEpoch = 0;
  for (size_t index = 0; index < slots.size(); ++index) {
    const JsonVariantConst rawSlot = slots[index];
    if (!rawSlot.is<JsonObjectConst>()) {
      lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE";
      lastDeviceErrorMessage = "離線排程 active Slot 身分不合法";
      return false;
    }
    const JsonObjectConst slot = rawSlot.as<JsonObjectConst>();
    const JsonVariantConst rawIndex = slot["slot_index"];
    const JsonVariantConst rawVersion = slot["queue_version"];
    const JsonVariantConst rawShowAtEpoch = slot["show_at_epoch"];
    const String slotScheduleId = slot["offline_schedule_id"] | "";
    const String slotItemId = slot["queue_item_id"] | "";
    const String slotReleaseId = slot["release_id"] | "";
    const String slotSha = slot["sha256"] | "";
    const String slotProfile = slot["render_profile"] | "";
    if (!rawIndex.is<int32_t>() || rawIndex.is<bool>()
        || rawIndex.as<int32_t>() != static_cast<int32_t>(index)
        || !rawVersion.is<int32_t>() || rawVersion.is<bool>() || rawVersion.as<int32_t>() < 0
        || !rawShowAtEpoch.is<int64_t>() || rawShowAtEpoch.is<bool>()
        || rawShowAtEpoch.as<int64_t>() < targetStartEpoch
        || rawShowAtEpoch.as<int64_t>() >= targetEndEpoch
        || (index > 0U && rawShowAtEpoch.as<int64_t>() <= previousShowAtEpoch)
        || slotScheduleId != activeScheduleId
        || !inktime::boundedText(slotItemId.c_str(), inktime::kQueueIdentifierMaxBytes)
        || !inktime::boundedText(slotReleaseId.c_str(), inktime::kQueueIdentifierMaxBytes)
        || !inktime::isSha256Hex(slotSha.c_str())
        || (slotProfile != "safe_4c" && slotProfile != String(INKTIME_PANEL_PROFILE))) {
      lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE";
      lastDeviceErrorMessage = "離線排程 active Slot 身分或完整性不合法";
      return false;
    }
    previousShowAtEpoch = rawShowAtEpoch.as<int64_t>();
    previewEpochs[index] = previousShowAtEpoch;
    previewShaValues[index] = slotSha;
    previewShaPointers[index] = previewShaValues[index].c_str();
  }
  int selectedIndex = -1;
  int64_t selectedEpoch = selectNext ? INT64_MAX : 0;
  String selectedSha;
  String selectedRelease;
  String selectedProfile;
  String selectedQueueItemId;
  int32_t selectedQueueVersion = -1;
  if (selectNext) {
    const StoredDisplayRecord displayRecord = loadDisplayRecord();
    const int16_t selectedPreviewIndex = inktime::nextOfflinePreviewSlot(
      previewEpochs,
      previewShaPointers,
      static_cast<uint8_t>(slots.size()),
      previewCursor,
      static_cast<uint64_t>(nowEpoch),
      displayRecord.valid ? displayRecord.sha256.c_str() : "");
    if (selectedPreviewIndex < 0) {
      const int16_t nextCursor = previewCursor < 0
        ? 0
        : static_cast<int16_t>((previewCursor + 1) % slots.size());
      savePreviewCursor(activeScheduleId, nextCursor);
      lastDeviceErrorCode = "DEVICE-OFFLINE-PREVIEW";
      lastDeviceErrorMessage = "離線預覽沒有不同 SHA 的本地快取 Frame";
      return false;
    }
    const JsonObjectConst slot = slots[static_cast<size_t>(selectedPreviewIndex)].as<JsonObjectConst>();
    selectedIndex = selectedPreviewIndex;
    selectedEpoch = slot["show_at_epoch"] | 0;
    selectedSha = slot["sha256"] | "";
    selectedRelease = slot["release_id"] | "";
    selectedProfile = slot["render_profile"] | "";
    selectedQueueItemId = slot["queue_item_id"] | "";
    selectedQueueVersion = slot["queue_version"] | -1;
    savePreviewCursor(activeScheduleId, selectedPreviewIndex);
  }
  for (size_t index = 0; index < slots.size() && !selectNext; ++index) {
    const JsonVariantConst rawSlot = slots[index];
    if (!rawSlot.is<JsonObjectConst>()) continue;
    const JsonObjectConst slot = rawSlot.as<JsonObjectConst>();
    const int slotIndex = slot["slot_index"] | -1;
    if (slotIndex < 0 || slotIndex >= static_cast<int>(rawTimes.size())) continue;
    const JsonVariantConst rawShowAtEpoch = slot["show_at_epoch"];
    if (!rawShowAtEpoch.is<int64_t>() || rawShowAtEpoch.is<bool>()) continue;
    const int64_t candidateEpoch = rawShowAtEpoch.as<int64_t>();
    const String sha = slot["sha256"] | "";
    const bool candidateMatches = selectNext
      ? candidateEpoch > static_cast<int64_t>(nowEpoch) && candidateEpoch < selectedEpoch
      : candidateEpoch <= static_cast<int64_t>(nowEpoch) && candidateEpoch >= selectedEpoch;
    if (candidateMatches && inktime::isSha256Hex(sha.c_str())) {
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
  const inktime::DisplayRotation rotation = activeRotation.as<int32_t>() == 180
    ? inktime::DisplayRotation::Rotate180
    : inktime::DisplayRotation::Rotate0;
  currentReleaseId = selectedRelease;
  currentRenderProfile = selectedProfile;
  currentQueueItemId = selectedQueueItemId;
  currentQueueVersion = selectedQueueVersion;
  const inktime::OfflineDisplayIntent displayIntent = inktime::offlineDisplayIntent(selectNext);
  currentFromQueue = displayIntent.ownsFormalSlot && inktime::boundedText(
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

static void runOfflineLocalCycle(bool selectNext = false) {
  time_t rtcEpoch = 0;
  struct tm offlineTime = {};
  const bool hasOfflineTime = photoPainter.readRtc(rtcEpoch);
  if (hasOfflineTime) {
    struct timeval value = {rtcEpoch, 0};
    settimeofday(&value, nullptr);
    applyFixedTimezoneWithoutNtp(g_cfg.tz_offset_minutes);
    localtime_r(&rtcEpoch, &offlineTime);
  }
  const bool ok = loadOfflineScheduledLocalFrame(
    g_cfg, hasOfflineTime ? rtcEpoch : 0, selectNext);
  bool displayUpdated = false;
  if (ok) {
    if (currentDisplaySkipped) {
      displayUpdated = true;
    } else {
      initDisplay(g_cfg);
      displayUpdated = drawFromFrameData(g_cfg);
    }
    if (displayUpdated) saveDisplayRecord(g_cfg, true);
    if (displayUpdated && !selectNext) clearOfflineRetryState();
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

#if INKTIME_PHOTOPAINTER_ENABLED
  const bool offlineTxnRecovered = reconcilePendingScheduleConfigTransaction(g_cfg);
  if (offlineTxnRecovered && g_cfg.delivery_mode == "inktime_offline_schedule") {
    time_t bootRtcEpoch = 0;
    if (photoPainter.readRtc(bootRtcEpoch) && bootRtcEpoch > 0) {
      applyFixedTimezoneWithoutNtp(g_cfg.tz_offset_minutes);
      struct timeval value = {bootRtcEpoch, 0};
      settimeofday(&value, nullptr);
      // Reliable RTC time is the authority for the local-midnight boundary;
      // promote a fully validated staged-next snapshot before any local or
      // network display decision is made.
      (void)promoteStagedNextIfDue(g_cfg, bootRtcEpoch);
    }
  }
#endif

  if (!g_cfg.valid) {
#if DEBUG_LOG
    DBG_PRINTLN("[BOOT] no valid config -> AP portal");
#endif
    startConfigPortal();
  }

#if INKTIME_PHOTOPAINTER_ENABLED
  const esp_sleep_wakeup_cause_t wakeCause = esp_sleep_get_wakeup_cause();
  const bool timerWake = wakeCause == ESP_SLEEP_WAKEUP_TIMER;
  if (!offlineScheduleTxnBlocked && g_cfg.delivery_mode == "inktime_offline_schedule"
      && photoPainter.wokeFromUserButton()
      && g_cfg.button_wake_action == "local_next") {
    // local_next is a strict cache-only action.  It must not connect Wi-Fi or
    // ask the generic queue for its first item.
    runOfflineLocalCycle(true);
    return;
  }
  if (!offlineScheduleTxnBlocked && g_cfg.delivery_mode == "inktime_offline_schedule" && timerWake
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
      || activeHasDueFormalSlot(rtcEpoch)
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
      if (!offlineScheduleTxnBlocked && g_cfg.delivery_mode == "inktime_offline_schedule" && hasOfflineTime
          && activeHasDueFormalSlot(rtcEpoch)) {
        // A due 00:00/current formal slot is serviceable from the active
        // cache even when the midnight network recovery attempt fails.
        runOfflineLocalCycle();
        return;
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
#if INKTIME_PHOTOPAINTER_ENABLED
  if (ok && !offlineScheduleTxnBlocked && !currentPrefetchOnly
      && g_cfg.delivery_mode == "inktime_offline_schedule"
      && (displayUpdated || currentDisplaySkipped)) {
    clearOfflineRetryState();
  }
#endif
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
