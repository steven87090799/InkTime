#include <WiFi.h>
#include <stddef.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <SPI.h>
#include <time.h>
#include <sys/time.h>
#include "esp_heap_caps.h"
#include "esp_sntp.h"
#include "esp_system.h"
#include "esp_attr.h"

#include "hardware_profile.h"
#include "photopainter_core.h"
#include "offline_schedule_core.h"
#include "device_config_store.h"
#include "pairing_recovery_core.h"
#include "max_awake_recovery_core.h"
#include "queue_client_core.h"
#include "queue_runtime_types.h"
#include "ack_journal_transaction_core.h"
#include "ack_journal_storage_budget.h"

// Arduino's prototype generator runs before later .ino declarations.  Keep
// these metadata types visible to generated CRC helper prototypes.
struct AckJournalSnapshotMeta;
struct AckJournalActivePointer;
struct Config;

#include "device_http_transport.h"
#if INKTIME_PHOTOPAINTER_ENABLED
#include "photopainter_support.h"
#include "power_manager.h"
#else
#include <GxEPD2_7C.h>
#endif
#include "esp_wifi.h"
#include "esp_bt.h"
#include "mbedtls/sha256.h"
#include "mbedtls/version.h"

#include "driver/gpio.h"
#include "soc/soc_caps.h"
#if INKTIME_PHOTOPAINTER_ENABLED
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#endif

// =======================
//  正式版預設不輸出逐步序列 Log；需要除錯時以 -DINKTIME_DEBUG_LOG=1 編譯。
// =======================
#ifndef INKTIME_DEBUG_LOG
#define INKTIME_DEBUG_LOG 0
#endif
#define DEBUG_LOG INKTIME_DEBUG_LOG

#include "firmware_observability.h"

using inktime::kBoardConfig;

#if INKTIME_PHOTOPAINTER_ENABLED
inktime::PhotoPainterSupport photoPainter(kBoardConfig);
#endif

#if DEBUG_LOG
  #define DBG_BEGIN()    INK_LOG_BEGIN()
  #define DBG_PRINT(x)   Serial.print(x)
  #define DBG_PRINTLN(x) Serial.println(x)
#else
  #define DBG_BEGIN()    INK_LOG_BEGIN()
  #define DBG_PRINT(x)
  #define DBG_PRINTLN(x)
#endif

static const uint32_t FACTORY_RESET_SAMPLE_DELAY_MS = 5;

// =======================
//  AP 配置页保底：进入 AP 后 5 分钟没保存配置 -> 睡到“下一个刷新点”
// =======================
static const uint32_t AP_TIMEOUT_MS = 5UL * 60UL * 1000UL; // 5 分钟
static const uint8_t AP_MAX_SAVE_ATTEMPTS = 5;

#if INKTIME_PHOTOPAINTER_ENABLED
static constexpr uint32_t kMaxAwakeTimeoutMs = 10UL * 60UL * 1000UL;
static constexpr uint32_t kMaxAwakeSupervisorStackBytes = 2048U;
static constexpr uint32_t kMaxAwakeCommandArm = 1U;
static constexpr uint32_t kMaxAwakeCommandDisarm = 2U;
static TaskHandle_t maxAwakeSupervisorTaskHandle = nullptr;
static bool maxAwakeSupervisorCreated = false;
RTC_NOINIT_ATTR static inktime::MaxAwakeRecoveryState maxAwakeRecoveryState;

static void maxAwakeSupervisorTask(void*) {
  bool armed = true;
  for (;;) {
    uint32_t command = 0U;
    const TickType_t waitTicks = armed
        ? pdMS_TO_TICKS(kMaxAwakeTimeoutMs)
        : portMAX_DELAY;
    if (xTaskNotifyWait(0U, UINT32_MAX, &command, waitTicks) != pdTRUE) {
      if (armed) {
        (void)inktime::recordMaxAwakeTimeout(maxAwakeRecoveryState);
        esp_restart();
      }
      continue;
    }
    if (command == kMaxAwakeCommandDisarm) {
      armed = false;
    } else if (command == kMaxAwakeCommandArm) {
      armed = true;
    }
  }
}

static void startMaxAwakeSupervisor() {
  if (maxAwakeSupervisorCreated) return;
  const BaseType_t created = xTaskCreate(
    maxAwakeSupervisorTask,
    "ink_maxawake",
    kMaxAwakeSupervisorStackBytes,
    nullptr,
    tskIDLE_PRIORITY + 1U,
    &maxAwakeSupervisorTaskHandle
  );
  if (created == pdPASS) {
    maxAwakeSupervisorCreated = true;
  } else {
    maxAwakeSupervisorTaskHandle = nullptr;
    maxAwakeSupervisorCreated = false;
  }
}

static void disarmMaxAwakeSupervisor() {
  if (maxAwakeSupervisorTaskHandle == nullptr) return;
  xTaskNotify(
    maxAwakeSupervisorTaskHandle,
    kMaxAwakeCommandDisarm,
    eSetValueWithOverwrite
  );
}

static void armMaxAwakeSupervisor() {
  if (maxAwakeSupervisorTaskHandle == nullptr) return;
  xTaskNotify(
    maxAwakeSupervisorTaskHandle,
    kMaxAwakeCommandArm,
    eSetValueWithOverwrite
  );
}
#endif

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
#define DEVICE_QUEUE_ACK_BATCH_PATH "/api/device/v1/queue/acks"
#define DEVICE_OFFLINE_SCHEDULE_PATH "/api/device/v1/offline-schedule"
#define DEVICE_PAIRING_REQUEST_PATH "/api/device/v1/pairing/request"
#define DEVICE_PAIRING_CLAIM_PATH "/api/device/v1/pairing/claim"
#define DEVICE_PAIRING_CONFIRM_PATH "/api/device/v1/pairing/confirm"
#define DEVICE_PAIRING_REPAIR_PERMISSION_PATH "/api/device/v1/pairing/repair-permission"
#define INKTIME_FIRMWARE_VERSION "2.8.1"

static constexpr uint8_t kQueueAckBatchMaxEvents = 8U;
static constexpr size_t kQueueAckBatchMaxBodyBytes = 12U * 1024U;

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

struct RuntimeTelemetry {
  uint32_t wifi_connect_ms = 0;
  bool wifi_fast_path_attempted = false;
  bool wifi_fast_path_success = false;
  uint32_t network_session_ms = 0;
  uint32_t http_request_count = 0;
  uint32_t ntp_sync_ms = 0;
  bool ntp_sync_attempted = false;
  bool ntp_sync_succeeded = false;
  uint32_t download_bytes = 0;
  uint32_t nvs_write_count = 0;
  uint32_t ack_event_count = 0;
  uint32_t ack_batch_request_count = 0;
  uint32_t i2c_retry_count = 0;
  uint32_t i2c_bus_reset_count = 0;
  uint32_t i2c_fail_closed_count = 0;
  uint32_t gc_deleted_files = 0;
  uint32_t gc_deleted_bytes = 0;
  uint32_t gc_skipped_protected = 0;
  uint32_t epd_transfer_ms = 0;
  uint32_t applied_offline_schedule_version = 0;
  int64_t next_wake_epoch = 0;
  int64_t next_network_sync_epoch = 0;
  bool tls_handshake_count_unavailable = true;
  String tls_handshake_count_unavailable_reason =
      "transport_api_does_not_expose_handshake_count";
};

RuntimeTelemetry runtimeTelemetry;
static uint32_t networkSessionStartedMs = 0;
static constexpr uint64_t kPairingMinimumEpoch = 1700000000ULL;

struct Config {
  String  wifi_ssid;
  String  wifi_pass;
  String  backend_hostport;
  String  ca_pem;
  String  device_token;
  String  device_secret;
  String  device_id;
  String  auth_state;
  uint32_t credential_version;
  String  pairing_id;
  String  pairing_nonce;
  uint64_t pairing_expires_at_epoch;
  uint64_t pairing_retry_at_epoch;
  uint8_t pairing_retry_attempt;
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
  String  sync_strategy;
  String  sync_time;
  uint32_t config_version;
  bool    valid;
};

// One transport object is shared by safe requests during a network wake.  It
// is deliberately closed before any EPD refresh so the radio cannot remain
// active while the panel is drawing power.
static inktime::DeviceHttpTransport wakeHttpTransport;
static String wakeHttpOrigin;
static bool wakeHttpSessionOpen = false;
static bool networkClosedForDisplay = false;

static void closeWakeHttpSession();
static void stopNetworkBeforeDisplay();

static void recordNvsWrite() {
  if (runtimeTelemetry.nvs_write_count < UINT32_MAX) ++runtimeTelemetry.nvs_write_count;
}

static int countedHttpGet(HTTPClient& http) {
  if (runtimeTelemetry.http_request_count < UINT32_MAX) ++runtimeTelemetry.http_request_count;
  return http.GET();
}

static int countedHttpPost(HTTPClient& http, const String& body) {
  if (runtimeTelemetry.http_request_count < UINT32_MAX) ++runtimeTelemetry.http_request_count;
  return http.POST(body);
}

static constexpr const char* kPairingRetryNamespace = "pairing_retry";
static constexpr const char* kPairingRetryAttemptKey = "attempt";
static constexpr const char* kPairingRetryDeadlineKey = "deadline";

class PairingRetryMetadataStore final {
 public:
  bool load(
      inktime::pairing::RetryState& state,
      bool& present,
      String& error) const {
    state = inktime::pairing::RetryState{};
    present = false;
    error = "";
    Preferences storage;
    if (!storage.begin(kPairingRetryNamespace, true)) {
      error = "PAIRING-NVS-008";
      return false;
    }
    const bool has_attempt = storage.isKey(kPairingRetryAttemptKey);
    const bool has_deadline = storage.isKey(kPairingRetryDeadlineKey);
    if (!has_attempt && !has_deadline) {
      storage.end();
      return true;
    }
    if (!has_attempt || !has_deadline) {
      storage.end();
      error = "PAIRING-NVS-008";
      return false;
    }
    state.attempt = storage.getUChar(kPairingRetryAttemptKey, 0U);
    uint64_t deadline = 0U;
    const size_t read_size = storage.getBytes(
      kPairingRetryDeadlineKey, &deadline, sizeof(deadline));
    storage.end();
    if (read_size != sizeof(deadline)
        || state.attempt > inktime::pairing::kMaximumRetryAttempt) {
      error = "PAIRING-NVS-008";
      return false;
    }
    state.retry_at_epoch = deadline;
    present = true;
    return true;
  }

  bool save(const inktime::pairing::RetryState& state, String& error) const {
    error = "";
    Preferences storage;
    if (!storage.begin(kPairingRetryNamespace, false)) {
      error = "PAIRING-NVS-008";
      return false;
    }
    const size_t attempt_size = storage.putUChar(kPairingRetryAttemptKey, state.attempt);
    const size_t deadline_size = storage.putBytes(
      kPairingRetryDeadlineKey, &state.retry_at_epoch, sizeof(state.retry_at_epoch));
    if (attempt_size == sizeof(state.attempt)) recordNvsWrite();
    if (deadline_size == sizeof(state.retry_at_epoch)) recordNvsWrite();
    const uint8_t read_attempt = storage.getUChar(kPairingRetryAttemptKey, 0U);
    uint64_t read_deadline = 0U;
    const size_t read_size = storage.getBytes(
      kPairingRetryDeadlineKey, &read_deadline, sizeof(read_deadline));
    storage.end();
    if (attempt_size != sizeof(state.attempt)
        || deadline_size != sizeof(state.retry_at_epoch)
        || read_size != sizeof(read_deadline)
        || read_attempt != state.attempt
        || read_deadline != state.retry_at_epoch) {
      error = "PAIRING-NVS-008";
      return false;
    }
    return true;
  }

  bool clear(String& error) const {
    error = "";
    Preferences storage;
    if (!storage.begin(kPairingRetryNamespace, false)) {
      error = "PAIRING-NVS-008";
      return false;
    }
    bool ok = true;
    if (storage.isKey(kPairingRetryAttemptKey)) {
      const bool removed = storage.remove(kPairingRetryAttemptKey);
      if (removed) recordNvsWrite();
      else ok = false;
    }
    if (storage.isKey(kPairingRetryDeadlineKey)) {
      const bool removed = storage.remove(kPairingRetryDeadlineKey);
      if (removed) recordNvsWrite();
      else ok = false;
    }
    const bool empty = !storage.isKey(kPairingRetryAttemptKey)
      && !storage.isKey(kPairingRetryDeadlineKey);
    storage.end();
    if (!ok || !empty) {
      error = "PAIRING-NVS-008";
      return false;
    }
    return true;
  }
};

const char*  DEFAULT_HOSTPORT = "";
const int32_t DEFAULT_TZ_MINUTES = 8 * 60;
const uint8_t DEFAULT_HOUR    = 8;
const uint8_t DEFAULT_MINUTE  = 0;

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
  char next[11] = {0};
  if (!inktime::nextIsoLocalDateValue(value.c_str(), next, sizeof(next))) return false;
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

static bool validSyncTime(const String &value) {
  if (value.isEmpty()) return true;
  if (value.length() != 5U || value[2] != ':') return false;
  if (value[0] < '0' || value[0] > '9' || value[1] < '0' || value[1] > '9'
      || value[3] < '0' || value[3] > '9' || value[4] < '0' || value[4] > '9') {
    return false;
  }
  const int hour = value.substring(0, 2).toInt();
  const int minute = value.substring(3, 5).toInt();
  return hour >= 0 && hour < 24 && minute >= 0 && minute < 60;
}

static bool validSyncStrategy(const String &strategy, const String &syncTime) {
  if (strategy != "first_display_lead" && strategy != "fixed_daily") return false;
  if (strategy == "first_display_lead") return syncTime.isEmpty();
  return !syncTime.isEmpty() && validSyncTime(syncTime);
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
  String syncStrategy = remoteConfig["sync_strategy"] | candidate.sync_strategy;
  String syncTime = remoteConfig["sync_time"] | candidate.sync_time;
  const JsonVariantConst leadValue = remoteConfig["prefetch_lead_minutes"];
  if (!leadValue.isNull() && (!leadValue.is<int>() || leadValue.is<bool>())) return false;
  int lead = leadValue.isNull() ? static_cast<int>(candidate.prefetch_lead_minutes) : (leadValue | -1);
  if (!validDeliveryMode(delivery) || !validButtonWakeAction(button)
      || !validSyncStrategy(syncStrategy, syncTime) || lead < 0 || lead > 120) return false;

  JsonArray rawTimes = remoteConfig["schedule_times"].as<JsonArray>();
  if (rawTimes.isNull() || rawTimes.size() == 0U || rawTimes.size() > inktime::kMaxOfflineSlots) return false;
  inktime::OfflineSlot slots[inktime::kMaxOfflineSlots] = {};
  for (size_t index = 0; index < rawTimes.size(); ++index) {
    if (!parseOfflineClock(rawTimes[index] | "", slots[index])) return false;
  }
  if (!inktime::validateOfflineSlots(slots, static_cast<uint8_t>(rawTimes.size()))) return false;
  candidate.delivery_mode = delivery;
  candidate.button_wake_action = button;
  candidate.sync_strategy = syncStrategy;
  candidate.sync_time = syncTime;
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
bool deviceAuthInvalid = false;
bool offlineDeliveryModeMismatchDetected = false;
#if !INKTIME_PHOTOPAINTER_ENABLED
static bool displayPairingCode(const Config &cfg, const String &pairingCode);
#endif
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
String lastDeviceWarningCode;
String lastDeviceWarningMessage;
bool queueAckPermanentReject = false;
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

static void clearConfigNVS() {
#if DEBUG_LOG
  DBG_PRINTLN("[NVS] clearConfigNVS()");
#endif
  String storeError;
  bool storeCleared = configStore.clearAll(storeError);
  if (storeCleared) recordNvsWrite();
  PairingRetryMetadataStore retryStore;
  String retryError;
  if (!retryStore.clear(retryError)) {
    storeCleared = false;
    if (storeError.length() == 0U) storeError = retryError;
  }
  bool legacyCleared = false;
  if (prefs.begin("dashcfg", false)) {
    legacyCleared = prefs.clear();
    prefs.end();
    if (legacyCleared) recordNvsWrite();
    if (legacyCleared && prefs.begin("dashcfg", true)) {
      const char* keys[] = {
        "last_epoch", "last_ntp", "wifi_bssid", "wifi_channel",
        "offretry_attempt", "offretry_epoch", "offretry_next",
        "ack_item", "ack_version", "ack_event", "ack_attempt", "ack_next",
        "ssid", "pass", "hostport", "ca_pem", "devtoken", "tzmin", "tz",
        "hour", "minute", "rot180", "prefetch", "delivery", "button", "cfgver", "scnt",
      };
      for (const char* key : keys) {
        if (prefs.isKey(key)) {
          legacyCleared = false;
          break;
        }
      }
      prefs.end();
    } else {
      legacyCleared = false;
    }
  }
  if (!storeCleared || !legacyCleared) {
    lastDeviceErrorCode = "PAIRING-NVS-006";
    lastDeviceErrorMessage = storeError.length() > 0U ? storeError : "NVS clear/readback 失敗";
  }
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
  const size_t written = prefs.putULong("last_epoch", (uint32_t)epoch);
  prefs.end();
  if (written > 0U) recordNvsWrite();
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
  const size_t attemptWritten = prefs.putUChar(
    "offretry_attempt", attempt > 2U ? 2U : attempt);
  const size_t epochWritten = prefs.putLong64("offretry_epoch", epoch);
  const size_t nextWritten = prefs.putLong64(
    "offretry_next", nextSlotEpoch > 0 ? nextSlotEpoch : 0);
  prefs.end();
  if (attemptWritten > 0U) recordNvsWrite();
  if (epochWritten > 0U) recordNvsWrite();
  if (nextWritten > 0U) recordNvsWrite();
}

static void clearOfflineRetryState() {
  prefs.begin("dashcfg", false);
  if (prefs.remove("offretry_attempt")) recordNvsWrite();
  if (prefs.remove("offretry_epoch")) recordNvsWrite();
  if (prefs.remove("offretry_next")) recordNvsWrite();
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
    const size_t scheduleWritten = prefs.putString("preview_sched", scheduleId);
    const size_t indexWritten = prefs.putInt("preview_idx", -1);
    prefs.end();
    if (scheduleWritten > 0U) recordNvsWrite();
    if (indexWritten > 0U) recordNvsWrite();
    return -1;
  }
  return storedIndex < 0 || storedIndex > 127 ? -1 : static_cast<int16_t>(storedIndex);
}

static void savePreviewCursor(const String &scheduleId, int16_t slotIndex) {
  prefs.begin("dashcfg", false);
  const size_t scheduleWritten = prefs.putString("preview_sched", scheduleId);
  const size_t indexWritten = prefs.putInt("preview_idx", slotIndex);
  prefs.end();
  if (scheduleWritten > 0U) recordNvsWrite();
  if (indexWritten > 0U) recordNvsWrite();
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
  const size_t written = prefs.putULong("photo_idx", static_cast<uint32_t>(index));
  prefs.end();
  if (written > 0U) recordNvsWrite();
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

#if INKTIME_PHOTOPAINTER_ENABLED
static void runFormalFrameGcForWake() {
  String activeScheduleJson;
  String stagedNextScheduleJson;
  (void)photoPainter.readActiveSchedule(activeScheduleJson);
  (void)photoPainter.readStagedNextSchedule(stagedNextScheduleJson);
  const StoredDisplayRecord lastGood = loadDisplayRecord();
  const char* lastGoodSha256 = lastGood.valid && lastGood.succeeded
    ? lastGood.sha256.c_str()
    : nullptr;
  const char* recoverySha256 = offlineScheduleTxnBlocked
    && inktime::isSha256Hex(currentPayloadSha256.c_str())
    ? currentPayloadSha256.c_str()
    : nullptr;
  // Formal Frame GC is best-effort.  Transactional .tmp/.bak artifacts remain
  // outside the candidate set, while the active/staged/current/last-good and
  // recovery references are explicit protection fences.
  (void)photoPainter.runFormalFrameGc(
    activeScheduleJson.c_str(),
    stagedNextScheduleJson.c_str(),
    currentPayloadSha256.c_str(),
    lastGoodSha256,
    photoPainter.inFlightFormalFrameSha256(),
    recoverySha256);
}
#endif

static void saveDisplayRecord(const Config &cfg, bool succeeded) {
  if (!inktime::isSha256HexValue(currentPayloadSha256.c_str())
      || currentReleaseId.length() == 0U || currentReleaseId.length() > 128U
      || currentRenderProfile.length() == 0U || currentRenderProfile.length() > 64U) {
    return;
  }
  prefs.begin("dashcfg", false);
  const size_t versionWritten = prefs.putUChar("disp_ver", 1U);
  const size_t shaWritten = prefs.putString("last_sha", currentPayloadSha256);
  const size_t releaseWritten = prefs.putString("last_rel", currentReleaseId);
  const size_t profileWritten = prefs.putString("last_prof", currentRenderProfile);
  const size_t boardWritten = prefs.putString("last_board", kBoardConfig.name);
  const size_t rotationWritten = prefs.putShort("last_rot", cfg.rotate180 ? 180 : 0);
  const size_t successWritten = prefs.putBool("last_ok", succeeded);
  prefs.end();
  if (versionWritten > 0U) recordNvsWrite();
  if (shaWritten > 0U) recordNvsWrite();
  if (releaseWritten > 0U) recordNvsWrite();
  if (profileWritten > 0U) recordNvsWrite();
  if (boardWritten > 0U) recordNvsWrite();
  if (rotationWritten > 0U) recordNvsWrite();
  if (successWritten > 0U) recordNvsWrite();
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

static String ackJournalBlobKey(uint8_t index) {
  return String("b") + String(index);
}

static String ackJournalBankKey(char bank, uint8_t index) {
  return String(bank) + String(index);
}

static String ackJournalSnapshotMetaKey(char bank) {
  return String("meta_") + String(bank);
}

struct __attribute__((packed)) AckJournalBlob {
  uint32_t magic;
  uint8_t version;
  uint8_t flags;
  int32_t queue_version;
  uint8_t event;
  uint8_t reserved;
  int64_t event_epoch;
  uint16_t item_length;
  uint16_t error_length;
  uint16_t release_length;
  char queue_item_id[inktime::kQueueIdentifierMaxBytes + 1U];
  char error_code[65U];
  char release_id[inktime::kQueueIdentifierMaxBytes + 1U];
  uint32_t crc32;
};

struct __attribute__((packed)) AckJournalSnapshotMeta {
  uint32_t magic;
  uint8_t version;
  uint8_t bank;
  uint8_t count;
  uint8_t reserved;
  uint64_t generation;
  uint32_t content_crc32;
  uint32_t crc32;
};

struct __attribute__((packed)) AckJournalActivePointer {
  uint32_t magic;
  uint8_t version;
  uint8_t bank;
  uint8_t count;
  uint8_t reserved;
  uint64_t generation;
  uint32_t crc32;
};

// The ACK namespace keeps two copy-on-write banks (G/H), one active pointer,
// and the legacy b0..b31/count representation during forward migration.
// Each bank is at most 32 compact blobs, so the worst-case peak is two banks
// plus one legacy generation.  The bounded blob size is intentionally kept
// below the ESP32 Preferences value limit; the repository-owned partition
// tables reserve 512 KiB of NVS and the shared budget header proves the peak
// including ConfigStore A/B, large-CA, retry/config metadata, migration, and
// safety margin.  The stock 20 KiB app3M_fat9M_16MB table is not valid for
// this firmware and must not be selected.

static constexpr uint32_t kAckJournalBlobMagic = 0x49544A31U;
static constexpr uint8_t kAckJournalBlobVersion = 1U;
static constexpr uint32_t kAckJournalSnapshotMagic = 0x49544A32U;
static constexpr uint8_t kAckJournalSnapshotVersion = 1U;
static constexpr uint32_t kAckJournalPointerMagic = 0x49544A33U;
static constexpr uint8_t kAckJournalPointerVersion = 1U;
static constexpr size_t kAckJournalPreferencesValueLimitBytes = 1984U;
static constexpr size_t kAckJournalPeakRecordBytes =
  inktime::ackjournal::kAckJournalPeakRecordBytes;
static constexpr size_t kAckJournalWorstCaseNvsBytes =
  inktime::ackjournal::kWorstCaseNvsBytes;
static_assert(
  sizeof(AckJournalBlob) == inktime::ackjournal::kAckJournalBlobBytes,
  "ACK journal budget must track the packed firmware blob");
static_assert(
  sizeof(AckJournalSnapshotMeta) == inktime::ackjournal::kAckJournalSnapshotMetaBytes,
  "ACK journal budget must track snapshot metadata");
static_assert(
  sizeof(AckJournalActivePointer) == inktime::ackjournal::kAckJournalActivePointerBytes,
  "ACK journal budget must track the active pointer");
static_assert(
  sizeof(AckJournalBlob) <= kAckJournalPreferencesValueLimitBytes,
  "ACK journal blob must remain within one Preferences/NVS value");
static_assert(
  kAckJournalWorstCaseNvsBytes <= inktime::ackjournal::kTargetNvsPartitionBytes,
  "ACK journal/config migration peak must fit the target NVS partition");

static uint32_t ackJournalCrcUpdate(
  uint32_t crc, const uint8_t *bytes, size_t length) {
  for (size_t index = 0; index < length; ++index) {
    crc ^= bytes[index];
    for (uint8_t bit = 0; bit < 8U; ++bit) {
      crc = (crc & 1U) != 0U ? (crc >> 1U) ^ 0xEDB88320U : crc >> 1U;
    }
  }
  return crc;
}

static uint32_t ackJournalCrcBytes(const uint8_t *bytes, size_t length) {
  return ackJournalCrcUpdate(0xFFFFFFFFU, bytes, length) ^ 0xFFFFFFFFU;
}

static uint32_t ackJournalCrc(const AckJournalBlob &blob) {
  return ackJournalCrcBytes(
    reinterpret_cast<const uint8_t *>(&blob), offsetof(AckJournalBlob, crc32));
}

static uint32_t ackJournalMetaCrc(const AckJournalSnapshotMeta &meta) {
  return ackJournalCrcBytes(
    reinterpret_cast<const uint8_t *>(&meta), offsetof(AckJournalSnapshotMeta, crc32));
}

static uint32_t ackJournalPointerCrc(const AckJournalActivePointer &pointer) {
  return ackJournalCrcBytes(
    reinterpret_cast<const uint8_t *>(&pointer), offsetof(AckJournalActivePointer, crc32));
}

static bool copyAckJournalText(char *destination, size_t capacity, const String &value) {
  if (destination == nullptr || value.length() >= capacity) return false;
  memcpy(destination, value.c_str(), value.length());
  destination[value.length()] = '\0';
  return true;
}

static bool encodeAckJournalBlob(
  const PendingQueueAck &pending,
  AckJournalBlob &blob
) {
  blob = {};
  blob.magic = kAckJournalBlobMagic;
  blob.version = kAckJournalBlobVersion;
  blob.flags = (pending.displaySkipped ? 0x01U : 0U)
    | (pending.delayedTerminal ? 0x02U : 0U);
  blob.queue_version = pending.queueVersion;
  blob.event = static_cast<uint8_t>(pending.event);
  blob.event_epoch = pending.eventEpoch;
  blob.item_length = static_cast<uint16_t>(pending.queueItemId.length());
  blob.error_length = static_cast<uint16_t>(pending.errorCode.length());
  blob.release_length = static_cast<uint16_t>(pending.releaseId.length());
  if (!pending.valid
      || !copyAckJournalText(blob.queue_item_id, sizeof(blob.queue_item_id), pending.queueItemId)
      || !copyAckJournalText(blob.error_code, sizeof(blob.error_code), pending.errorCode)
      || !copyAckJournalText(blob.release_id, sizeof(blob.release_id), pending.releaseId)) {
    lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL";
    lastDeviceErrorMessage = "ACK journal compact blob 欄位超出安全容量";
    return false;
  }
  blob.crc32 = ackJournalCrc(blob);
  return true;
}

static bool decodeAckJournalBlob(
  const AckJournalBlob &blob,
  PendingQueueAck &pending
) {
  if (blob.magic != kAckJournalBlobMagic
      || blob.version != kAckJournalBlobVersion
      || blob.crc32 != ackJournalCrc(blob)
      || blob.item_length >= sizeof(blob.queue_item_id)
      || blob.error_length >= sizeof(blob.error_code)
      || blob.release_length >= sizeof(blob.release_id)
      || blob.queue_item_id[blob.item_length] != '\0'
      || blob.error_code[blob.error_length] != '\0'
      || blob.release_id[blob.release_length] != '\0') {
    return false;
  }
  pending = {
    String(blob.queue_item_id),
    blob.queue_version,
    static_cast<inktime::QueueEvent>(blob.event),
    (blob.flags & 0x01U) != 0U,
    String(blob.error_code),
    (blob.flags & 0x02U) != 0U,
    String(blob.release_id),
    blob.event_epoch,
    false,
  };
  return validPendingQueueAck(pending);
}

static PendingQueueAck readAckJournalEntry(Preferences &journal, uint8_t index) {
  PendingQueueAck pending = {};
  const String blobKey = ackJournalBlobKey(index);
  const size_t blobLength = journal.getBytesLength(blobKey.c_str());
  if (blobLength > 0U) {
    AckJournalBlob blob = {};
    if (blobLength == sizeof(blob)
        && journal.getBytes(blobKey.c_str(), &blob, sizeof(blob)) == sizeof(blob)
        && decodeAckJournalBlob(blob, pending)) {
      return pending;
    }
    lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL";
    lastDeviceErrorMessage = "ACK journal compact blob CRC／readback 驗證失敗";
    return pending;
  }
  // Read the pre-blob per-field representation for one-time migration.
  pending = {
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

static bool samePendingQueueAck(
  const PendingQueueAck &left,
  const PendingQueueAck &right
) {
  return left.queueItemId == right.queueItemId
    && left.queueVersion == right.queueVersion
    && left.event == right.event;
}

static bool terminalAckEvidence(const PendingQueueAck &pending) {
  return pending.delayedTerminal
    && (pending.event == inktime::QueueEvent::DisplayCompleted
      || pending.event == inktime::QueueEvent::DisplayFailed);
}

static bool readAckJournalBytes(
  Preferences &journal,
  const String &key,
  std::string &bytes
) {
  const size_t length = journal.getBytesLength(key.c_str());
  if (length == 0U) return false;
  bytes.assign(length, '\0');
  return journal.getBytes(key.c_str(), &bytes[0], length) == length;
}

static uint32_t ackJournalSnapshotContentCrc(
  char bank,
  uint64_t generation,
  const std::vector<std::string> &records
) {
  struct __attribute__((packed)) ContentHeader {
    uint8_t bank;
    uint8_t count;
    uint64_t generation;
  };
  const ContentHeader header = {
    static_cast<uint8_t>(bank),
    static_cast<uint8_t>(records.size()),
    generation,
  };
  uint32_t crc = 0xFFFFFFFFU;
  crc = ackJournalCrcUpdate(
    crc, reinterpret_cast<const uint8_t *>(&header), sizeof(header));
  for (const std::string &record : records) {
    crc = ackJournalCrcUpdate(
      crc, reinterpret_cast<const uint8_t *>(record.data()), record.size());
  }
  return crc ^ 0xFFFFFFFFU;
}

class AckJournalPreferencesStorage final : public inktime::ackjournal::Storage {
 public:
  explicit AckJournalPreferencesStorage(Preferences &journal) : journal_(journal) {}

  bool writeRecord(
    char bank,
    uint8_t index,
    const std::string &bytes
  ) override {
    if ((bank != 'G' && bank != 'H')
        || index >= inktime::kMaxAckJournalEntries
        || bytes.size() != sizeof(AckJournalBlob)) return false;
    const String key = ackJournalBankKey(bank, index);
    if (journal_.putBytes(key.c_str(), bytes.data(), bytes.size()) != bytes.size()) {
      lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL";
      lastDeviceErrorMessage = "ACK journal replacement blob 寫入失敗";
      return false;
    }
    std::string readback;
    const bool exact = readAckJournalBytes(journal_, key, readback) && readback == bytes;
    if (!exact) {
      lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL";
      lastDeviceErrorMessage = "ACK journal replacement blob exact readback 失敗";
      return false;
    }
    recordNvsWrite();
    return true;
  }

  bool writeSnapshotMetadata(
    char bank,
    uint64_t generation,
    const std::vector<std::string> &records
  ) override {
    if ((bank != 'G' && bank != 'H') || generation == 0U
        || records.size() > inktime::kMaxAckJournalEntries) return false;
    AckJournalSnapshotMeta meta = {};
    meta.magic = kAckJournalSnapshotMagic;
    meta.version = kAckJournalSnapshotVersion;
    meta.bank = static_cast<uint8_t>(bank);
    meta.count = static_cast<uint8_t>(records.size());
    meta.generation = generation;
    meta.content_crc32 = ackJournalSnapshotContentCrc(bank, generation, records);
    meta.crc32 = ackJournalMetaCrc(meta);
    const String key = ackJournalSnapshotMetaKey(bank);
    if (journal_.putBytes(key.c_str(), &meta, sizeof(meta)) != sizeof(meta)) {
      lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL";
      lastDeviceErrorMessage = "ACK journal snapshot metadata 寫入失敗";
      return false;
    }
    AckJournalSnapshotMeta readback = {};
    const bool exact = journal_.getBytesLength(key.c_str()) == sizeof(readback)
      && journal_.getBytes(key.c_str(), &readback, sizeof(readback)) == sizeof(readback)
      && memcmp(&readback, &meta, sizeof(meta)) == 0
      && readback.crc32 == ackJournalMetaCrc(readback);
    if (!exact) {
      lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL";
      lastDeviceErrorMessage = "ACK journal snapshot metadata exact readback 失敗";
      return false;
    }
    recordNvsWrite();
    return true;
  }

  bool writeActivePointer(char bank, uint64_t generation, uint8_t count) override {
    if ((bank != 'G' && bank != 'H') || generation == 0U
        || count > inktime::kMaxAckJournalEntries) return false;
    AckJournalActivePointer pointer = {};
    pointer.magic = kAckJournalPointerMagic;
    pointer.version = kAckJournalPointerVersion;
    pointer.bank = static_cast<uint8_t>(bank);
    pointer.count = count;
    pointer.generation = generation;
    pointer.crc32 = ackJournalPointerCrc(pointer);
    if (journal_.putBytes("active", &pointer, sizeof(pointer)) != sizeof(pointer)) {
      lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL";
      lastDeviceErrorMessage = "ACK journal active pointer 寫入失敗";
      return false;
    }
    AckJournalActivePointer readback = {};
    const bool exact = journal_.getBytesLength("active") == sizeof(readback)
      && journal_.getBytes("active", &readback, sizeof(readback)) == sizeof(readback)
      && memcmp(&readback, &pointer, sizeof(pointer)) == 0
      && readback.crc32 == ackJournalPointerCrc(readback);
    if (!exact) {
      lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL";
      lastDeviceErrorMessage = "ACK journal active pointer exact readback 失敗";
      return false;
    }
    recordNvsWrite();
    return true;
  }

  bool readSnapshot(
    char bank,
    inktime::ackjournal::Snapshot &snapshot,
    bool &present
  ) {
    present = false;
    const String metaKey = ackJournalSnapshotMetaKey(bank);
    if (journal_.getBytesLength(metaKey.c_str()) == 0U) return true;
    present = true;
    std::string metadata;
    if (!readAckJournalBytes(journal_, metaKey, metadata)
        || metadata.size() != sizeof(AckJournalSnapshotMeta)) return false;
    AckJournalSnapshotMeta meta = {};
    memcpy(&meta, metadata.data(), sizeof(meta));
    if (meta.magic != kAckJournalSnapshotMagic
        || meta.version != kAckJournalSnapshotVersion
        || meta.bank != static_cast<uint8_t>(bank)
        || meta.count > inktime::kMaxAckJournalEntries
        || meta.generation == 0U
        || meta.crc32 != ackJournalMetaCrc(meta)) return false;
    snapshot = {};
    snapshot.bank = bank;
    snapshot.generation = meta.generation;
    for (uint8_t index = 0U; index < meta.count; ++index) {
      std::string record;
      const String key = ackJournalBankKey(bank, index);
      if (!readAckJournalBytes(journal_, key, record)
          || record.size() != sizeof(AckJournalBlob)) return false;
      AckJournalBlob blob = {};
      memcpy(&blob, record.data(), sizeof(blob));
      if (blob.magic != kAckJournalBlobMagic
          || blob.version != kAckJournalBlobVersion
          || blob.crc32 != ackJournalCrc(blob)) return false;
      snapshot.records.push_back(record);
    }
    return ackJournalSnapshotContentCrc(
      bank, meta.generation, snapshot.records) == meta.content_crc32;
  }

  bool readActiveSnapshot(inktime::ackjournal::Snapshot &snapshot) {
    AckJournalActivePointer pointer = {};
    const bool pointerValid = journal_.getBytesLength("active") == sizeof(pointer)
      && journal_.getBytes("active", &pointer, sizeof(pointer)) == sizeof(pointer)
      && (pointer.bank == static_cast<uint8_t>('G')
        || pointer.bank == static_cast<uint8_t>('H'))
      && pointer.count <= inktime::kMaxAckJournalEntries
      && pointer.generation > 0U
      && pointer.magic == kAckJournalPointerMagic
      && pointer.version == kAckJournalPointerVersion
      && pointer.crc32 == ackJournalPointerCrc(pointer);
    if (pointerValid) {
      bool present = false;
      inktime::ackjournal::Snapshot pointed;
      if (readSnapshot(static_cast<char>(pointer.bank), pointed, present)
          && present && pointed.generation == pointer.generation
          && pointed.records.size() == pointer.count) {
        snapshot = pointed;
        return true;
      }
    }
    bool presentG = false;
    bool presentH = false;
    inktime::ackjournal::Snapshot candidateG;
    inktime::ackjournal::Snapshot candidateH;
    const bool validG = readSnapshot('G', candidateG, presentG) && presentG;
    const bool validH = readSnapshot('H', candidateH, presentH) && presentH;
    if (validG && validH) {
      // An invalid pointer can mean that the newer bank was fully prepared
      // but its authoritative pointer promotion tore.  The older complete
      // generation is the only fail-safe choice; replay is acceptable, ACK
      // evidence loss is not.  A successfully promoted bank remains safe
      // after cleanup because the previous bank is then no longer complete.
      snapshot = candidateG.generation <= candidateH.generation ? candidateG : candidateH;
      lastDeviceWarningCode = "DEVICE-QUEUE-ACK-RECOVERY";
      lastDeviceWarningMessage =
        "ACK journal active pointer 無效；已選擇較舊完整 generation 保留 at-least-once evidence";
      return true;
    }
    if (validG) {
      snapshot = candidateG;
      return true;
    }
    if (validH) {
      snapshot = candidateH;
      return true;
    }
    return false;
  }

  bool verifyActiveSnapshot(const inktime::ackjournal::Snapshot &expected) override {
    AckJournalActivePointer pointer = {};
    if (journal_.getBytesLength("active") != sizeof(pointer)
        || journal_.getBytes("active", &pointer, sizeof(pointer)) != sizeof(pointer)
        || pointer.bank != static_cast<uint8_t>(expected.bank)
        || pointer.generation != expected.generation
        || pointer.count != expected.records.size()
        || pointer.crc32 != ackJournalPointerCrc(pointer)) return false;
    inktime::ackjournal::Snapshot actual;
    bool present = false;
    if (!readSnapshot(expected.bank, actual, present) || !present) return false;
    return actual.bank == expected.bank
      && actual.generation == expected.generation
      && actual.records == expected.records;
  }

  bool cleanupBank(char bank) {
    bool ok = true;
    for (uint8_t index = 0U; index < inktime::kMaxAckJournalEntries; ++index) {
      const String key = ackJournalBankKey(bank, index);
      if (journal_.isKey(key.c_str())
          && !journal_.remove(key.c_str())) ok = false;
    }
    const String metaKey = ackJournalSnapshotMetaKey(bank);
    if (journal_.isKey(metaKey.c_str())
        && !journal_.remove(metaKey.c_str())) ok = false;
    return ok;
  }

  bool cleanupPrevious(char bank) override {
    if (bank != 'G' && bank != 'H') return true;
    const bool ok = cleanupBank(bank);
    if (!ok) {
      lastDeviceWarningCode = "DEVICE-QUEUE-ACK-CLEANUP";
      lastDeviceWarningMessage =
        "ACK journal 舊 generation cleanup 失敗；新 active snapshot 已保留";
    }
    return ok;
  }

 private:
  Preferences &journal_;
};

static bool legacyAckJournalPresent(Preferences &journal) {
  if (journal.getUChar("count", 0U) > 0U) return true;
  for (uint8_t index = 0U; index < inktime::kMaxAckJournalEntries; ++index) {
    if (journal.isKey(ackJournalBlobKey(index).c_str())
        || journal.getString(ackJournalKey('i', index).c_str(), "").length() > 0U) {
      return true;
    }
  }
  return false;
}

static bool removeLegacyAckJournalKeys(Preferences &journal) {
  bool ok = true;
  if (journal.isKey("count") && !journal.remove("count")) ok = false;
  const char legacyPrefixes[] = {'i', 'v', 'e', 's', 'r', 'd', 'l', 't'};
  for (uint8_t index = 0U; index < inktime::kMaxAckJournalEntries; ++index) {
    const String blobKey = ackJournalBlobKey(index);
    if (journal.isKey(blobKey.c_str()) && !journal.remove(blobKey.c_str())) ok = false;
    for (const char prefix : legacyPrefixes) {
      const String key = ackJournalKey(prefix, index);
      if (journal.isKey(key.c_str()) && !journal.remove(key.c_str())) ok = false;
    }
  }
  return ok && journal.getUChar("count", 0U) == 0U;
}

static bool loadAckJournalState(
  Preferences &journal,
  PendingQueueAck *entries,
  uint8_t &count,
  char &activeBank,
  uint64_t &generation,
  bool &legacyPresent
) {
  count = 0U;
  activeBank = 0;
  generation = 0U;
  legacyPresent = legacyAckJournalPresent(journal);
  AckJournalPreferencesStorage storage(journal);
  inktime::ackjournal::Snapshot snapshot;
  if (storage.readActiveSnapshot(snapshot)) {
    if (snapshot.records.size() > inktime::kMaxAckJournalEntries) return false;
    for (const std::string &record : snapshot.records) {
      AckJournalBlob blob = {};
      PendingQueueAck pending = {};
      if (record.size() != sizeof(blob)) return false;
      memcpy(&blob, record.data(), sizeof(blob));
      if (!decodeAckJournalBlob(blob, pending) || !pending.valid) return false;
      if (entries != nullptr) entries[count++] = pending;
    }
    activeBank = snapshot.bank;
    generation = snapshot.generation;
    return true;
  }
  uint8_t legacyCount = min(
    journal.getUChar("count", 0U),
    static_cast<uint8_t>(inktime::kMaxAckJournalEntries));
  if (legacyCount == 0U && legacyPresent) {
    for (uint8_t index = 0U; index < inktime::kMaxAckJournalEntries; ++index) {
      if (journal.isKey(ackJournalBlobKey(index).c_str())
          || journal.isKey(ackJournalKey('i', index).c_str())) {
        legacyCount = index + 1U;
      }
    }
  }
  for (uint8_t index = 0U; index < legacyCount; ++index) {
    PendingQueueAck pending = readAckJournalEntry(journal, index);
    if (pending.valid && entries != nullptr) entries[count++] = pending;
  }
  return true;
}

static bool commitAckJournalEntries(
  Preferences &journal,
  const PendingQueueAck *entries,
  uint8_t count,
  char previousBank,
  uint64_t previousGeneration,
  bool legacyPresent
) {
  if (entries == nullptr || count > inktime::kMaxAckJournalEntries
      || previousGeneration == UINT64_MAX) {
    lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL";
    lastDeviceErrorMessage = "ACK journal snapshot state 無效";
    return false;
  }
  inktime::ackjournal::Snapshot next;
  next.bank = previousBank == 'G' ? 'H' : 'G';
  next.generation = previousGeneration == 0U ? 1U : previousGeneration + 1U;
  for (uint8_t index = 0U; index < count; ++index) {
    AckJournalBlob blob = {};
    if (!encodeAckJournalBlob(entries[index], blob)) return false;
    next.records.emplace_back(reinterpret_cast<const char *>(&blob), sizeof(blob));
  }
  AckJournalPreferencesStorage storage(journal);
  std::string error;
  if (!inktime::ackjournal::commitSnapshot(storage, previousBank, next, error)) {
    lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL";
    lastDeviceErrorMessage = error.c_str();
    return false;
  }
  if (legacyPresent
      && inktime::ackjournal::legacyCleanupAllowed(true)
      && !removeLegacyAckJournalKeys(journal)) {
    lastDeviceWarningCode = "DEVICE-QUEUE-ACK-CLEANUP";
    lastDeviceWarningMessage = "ACK journal legacy cleanup 失敗；canonical snapshot 已保留";
  }
  return true;
}

static uint8_t loadAckJournalEntries(
  PendingQueueAck *entries,
  uint8_t capacity
) {
  if (entries == nullptr || capacity == 0U) return 0U;
  Preferences journal;
  if (!journal.begin("acklog", true)) return 0U;
  PendingQueueAck loadedEntries[inktime::kMaxAckJournalEntries] = {};
  uint8_t count = 0U;
  char activeBank = 0;
  uint64_t generation = 0U;
  bool legacyPresent = false;
  const bool loaded = loadAckJournalState(
    journal, loadedEntries, count, activeBank, generation, legacyPresent);
  (void)activeBank;
  (void)generation;
  (void)legacyPresent;
  if (!loaded) {
    journal.end();
    return 0U;
  }
  const uint8_t copied = min(count, capacity);
  for (uint8_t index = 0U; index < copied; ++index) entries[index] = loadedEntries[index];
  journal.end();
  return copied;
}

static bool removeLegacyPendingQueueAck() {
  prefs.begin("dashcfg", false);
  if (prefs.remove("ack_item")) recordNvsWrite();
  if (prefs.remove("ack_ver")) recordNvsWrite();
  if (prefs.remove("ack_event")) recordNvsWrite();
  if (prefs.remove("ack_skip")) recordNvsWrite();
  if (prefs.remove("ack_error")) recordNvsWrite();
  prefs.end();
  prefs.begin("dashcfg", true);
  const char *legacyKeys[] = {
    "ack_item", "ack_ver", "ack_event", "ack_skip", "ack_error"};
  bool absent = true;
  for (const char *key : legacyKeys) {
    if (prefs.isKey(key)) absent = false;
  }
  prefs.end();
  return absent;
}

static bool persistPendingQueueAck(const PendingQueueAck &pending) {
  if (!pending.valid) return false;
  Preferences journal;
  if (!journal.begin("acklog", false)) {
    lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL";
    lastDeviceErrorMessage = "ACK journal NVS namespace 無法開啟";
    return false;
  }
  PendingQueueAck current[inktime::kMaxAckJournalEntries] = {};
  uint8_t count = 0U;
  char activeBank = 0;
  uint64_t generation = 0U;
  bool legacyPresent = false;
  if (!loadAckJournalState(
        journal, current, count, activeBank, generation, legacyPresent)) {
    journal.end();
    lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL";
    lastDeviceErrorMessage = "ACK journal active snapshot 無法驗證";
    return false;
  }
  for (uint8_t index = 0; index < count; ++index) {
    const PendingQueueAck &existing = current[index];
    if (existing.valid && samePendingQueueAck(existing, pending)) {
      journal.end();
      return true;
    }
  }
  PendingQueueAck next[inktime::kMaxAckJournalEntries] = {};
  uint8_t nextCount = count;
  if (count >= inktime::kMaxAckJournalEntries) {
    uint8_t evictionIndex = count;
    for (uint8_t index = 0; index < count; ++index) {
      const PendingQueueAck &existing = current[index];
      if (existing.valid && !terminalAckEvidence(existing)) {
        evictionIndex = index;
        break;
      }
    }
    if (evictionIndex >= count) {
      // Terminal display evidence is the last class allowed to leave the
      // bounded journal.  If every entry is terminal, retain a visible
      // diagnostic while making the bounded forward-progress decision.
      evictionIndex = 0U;
      lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL-OVERFLOW";
      lastDeviceErrorMessage = "ACK journal 已滿，最舊 terminal display evidence 已淘汰";
    } else {
      lastDeviceWarningCode = "DEVICE-QUEUE-ACK-JOURNAL-COMPACTED";
      lastDeviceWarningMessage = "ACK journal 已滿，已優先淘汰可重建的 non-terminal ACK";
    }
    nextCount = 0U;
    for (uint8_t index = 0U; index < count; ++index) {
      if (index != evictionIndex) next[nextCount++] = current[index];
    }
  }
  if (nextCount >= inktime::kMaxAckJournalEntries) {
    journal.end();
    lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL";
    lastDeviceErrorMessage = "ACK journal replacement state 超過 bounded capacity";
    return false;
  }
  next[nextCount++] = pending;
  const bool committed = commitAckJournalEntries(
    journal, next, nextCount, activeBank, generation, legacyPresent);
  journal.end();
  return committed;
}

static bool removePendingQueueAck(const PendingQueueAck &pending) {
  Preferences journal;
  if (!journal.begin("acklog", false)) {
    lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL";
    lastDeviceErrorMessage = "ACK journal NVS namespace 無法開啟，無法移除已送出事件";
    return false;
  }
  PendingQueueAck current[inktime::kMaxAckJournalEntries] = {};
  uint8_t count = 0U;
  char activeBank = 0;
  uint64_t generation = 0U;
  bool legacyPresent = false;
  if (!loadAckJournalState(
        journal, current, count, activeBank, generation, legacyPresent)) {
    journal.end();
    lastDeviceErrorCode = "DEVICE-QUEUE-ACK-JOURNAL";
    lastDeviceErrorMessage = "ACK journal active snapshot 無法驗證，保留舊 generation";
    return false;
  }
  uint8_t found = count;
  for (uint8_t index = 0; index < count; ++index) {
    const PendingQueueAck &existing = current[index];
    if (existing.valid && samePendingQueueAck(existing, pending)) {
      found = index;
      break;
    }
  }
  if (found < count) {
    PendingQueueAck next[inktime::kMaxAckJournalEntries] = {};
    uint8_t nextCount = 0U;
    for (uint8_t index = 0U; index < count; ++index) {
      if (index != found) next[nextCount++] = current[index];
    }
    const bool committed = commitAckJournalEntries(
      journal, next, nextCount, activeBank, generation, legacyPresent);
    journal.end();
    return committed;
  }
  journal.end();
  return true;
}

static PendingQueueAck loadPendingQueueAck() {
  PendingQueueAck entries[inktime::kMaxAckJournalEntries] = {};
  const uint8_t count = loadAckJournalEntries(
    entries, inktime::kMaxAckJournalEntries);
  if (count > 0U) return entries[0];

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
    const bool canonicalCommitted = persistPendingQueueAck(legacy);
    if (inktime::ackjournal::legacyCleanupAllowed(canonicalCommitted)) {
      if (!removeLegacyPendingQueueAck()) {
        lastDeviceWarningCode = "DEVICE-QUEUE-ACK-CLEANUP";
        lastDeviceWarningMessage = "legacy single ACK cleanup 失敗；canonical snapshot 已保留";
      }
    }
    return legacy;
  }
  return legacy;
}

static void clearPendingQueueAck() {
  const PendingQueueAck pending = loadPendingQueueAck();
  if (pending.valid && !removePendingQueueAck(pending)) {
    lastDeviceWarningCode = "DEVICE-QUEUE-ACK-CLEANUP";
    lastDeviceWarningMessage = "ACK cleanup 未完成；duplicate idempotency ACK 將保留重送";
  }
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
  payload.device_secret = cfg.device_secret.c_str();
  payload.device_id = cfg.device_id.c_str();
  payload.auth_state = cfg.auth_state.c_str();
  payload.credential_version = cfg.credential_version;
  payload.pairing_id = cfg.pairing_id.c_str();
  payload.pairing_nonce = cfg.pairing_nonce.c_str();
  payload.pairing_expires_at_epoch = cfg.pairing_expires_at_epoch;
  payload.pairing_retry_at_epoch = cfg.pairing_retry_at_epoch;
  payload.pairing_retry_attempt = cfg.pairing_retry_attempt;
  payload.tz_offset_minutes = cfg.tz_offset_minutes;
  payload.refresh_hour = cfg.refresh_hour;
  payload.refresh_minute = cfg.refresh_minute;
  payload.rotate180 = cfg.rotate180;
  payload.sync_strategy = cfg.sync_strategy.c_str();
  payload.sync_time = cfg.sync_time.c_str();
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
  cfg.device_secret = payload.device_secret.c_str();
  cfg.device_id = payload.device_id.c_str();
  cfg.auth_state = payload.auth_state.c_str();
  cfg.credential_version = payload.credential_version;
  cfg.pairing_id = payload.pairing_id.c_str();
  cfg.pairing_nonce = payload.pairing_nonce.c_str();
  cfg.pairing_expires_at_epoch = payload.pairing_expires_at_epoch;
  cfg.pairing_retry_at_epoch = payload.pairing_retry_at_epoch;
  cfg.pairing_retry_attempt = payload.pairing_retry_attempt;
  if (cfg.auth_state.length() == 0U) {
    cfg.auth_state = cfg.device_secret.length() > 0U || cfg.device_token.length() > 0U
      ? "paired" : "unpaired";
  }
  cfg.tz_offset_minutes = payload.tz_offset_minutes;
  cfg.refresh_hour = payload.refresh_hour;
  cfg.refresh_minute = payload.refresh_minute;
  cfg.rotate180 = payload.rotate180;
  cfg.sync_strategy = payload.sync_strategy.c_str();
  cfg.sync_time = payload.sync_time.c_str();
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
  cfg.sync_strategy = "first_display_lead";
  cfg.sync_time = "";
  cfg.config_version = 0U;
  cfg.credential_version = 0U;
  cfg.auth_state = "unpaired";
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

static bool isRepairAuthState(const Config &cfg) {
  return cfg.auth_state == "auth_invalid" || cfg.auth_state == "revoked";
}

static inktime::pairing::RetryState retryStateFromConfig(const Config &cfg) {
  return {
    cfg.pairing_retry_at_epoch,
    cfg.pairing_retry_attempt,
  };
}

static void applyRetryStateToConfig(
    const inktime::pairing::RetryState& state,
    Config& cfg) {
  cfg.pairing_retry_at_epoch = state.retry_at_epoch;
  cfg.pairing_retry_attempt = state.attempt;
}

static void loadPairingRetryMetadata(Config& cfg) {
  if (!isRepairAuthState(cfg)) return;
  PairingRetryMetadataStore retryStore;
  inktime::pairing::RetryState state;
  bool present = false;
  String error;
  if (!retryStore.load(state, present, error)) {
    applyRetryStateToConfig(
      {0U, inktime::pairing::kMaximumRetryAttempt}, cfg);
    setConfigPersistenceError(error);
    return;
  }
  if (present) applyRetryStateToConfig(state, cfg);
}

void loadConfig(Config &cfg) {
  setConfigDefaults(cfg);
  inktime::configstore::ConfigPayload payload;
  String loadError;
  String loadWarning;
  if (configStore.load(payload, loadError, &loadWarning)) {
    applyConfigPayload(payload, cfg);
    if (loadWarning.length() > 0U) {
      lastDeviceWarningCode = "DEVICE-CONFIG-CLEANUP-PENDING";
      lastDeviceWarningMessage = "Canonical 設定已套用；舊設定清理待下次啟動重試：";
      lastDeviceWarningMessage += loadWarning;
    }
  } else if (loadError.length() > 0U) {
    setConfigPersistenceError(loadError);
  }
  cfg.valid = (cfg.wifi_ssid.length() > 0);
  loadPairingRetryMetadata(cfg);

#if DEBUG_LOG
  DBG_PRINTLN("---- loadConfig ----");
  DBG_PRINTLN("[CFG] WiFi SSID configured (value redacted)");
  DBG_PRINTLN("[CFG] backend endpoint configured (value redacted)");
  DBG_PRINT("[CFG] tz_offset_minutes="); DBG_PRINTLN(cfg.tz_offset_minutes);
  DBG_PRINT("[CFG] refresh_hour="); DBG_PRINTLN((int)cfg.refresh_hour);
  DBG_PRINT("[CFG] refresh_minute="); DBG_PRINTLN((int)cfg.refresh_minute);
  DBG_PRINT("[CFG] rotate180="); DBG_PRINTLN(cfg.rotate180 ? "true" : "false");
  DBG_PRINT("[CFG] valid="); DBG_PRINTLN(cfg.valid ? "true" : "false");
#endif
}

static bool verifyLegacyConfigReadback(const Config &cfg, String &error) {
  error = "";
  Preferences verify;
  if (!verify.begin("dashcfg", true)) {
    error = "PAIRING-NVS-002";
    return false;
  }
  const String verifiedCaPem = verify.getString("ca_pem", "");
  const String verifiedHostport = verify.getString("hostport", "");
  const String verifiedSsid = verify.getString("ssid", "");
  const bool legacyPresent = verifiedCaPem.length() > 0U
    || verifiedHostport.length() > 0U
    || verifiedSsid.length() > 0U;
  const bool matches = !legacyPresent
    || (verifiedCaPem == cfg.ca_pem
      && verifiedHostport == cfg.backend_hostport
      && verifiedSsid == cfg.wifi_ssid);
  verify.end();
  if (!matches) {
    error = "PAIRING-NVS-003";
    return false;
  }
  return true;
}

bool saveConfig(const Config &cfg, String *errorCodeOut = nullptr) {
  if (errorCodeOut != nullptr) *errorCodeOut = "";
  auto setError = [errorCodeOut](const char *code) {
    if (errorCodeOut != nullptr) *errorCodeOut = code;
  };
  if (cfg.ca_pem.length() > inktime::kMaxDeviceCaPemBytes
      || (cfg.ca_pem.length() > 0 && !inktime::DeviceHttpTransport::trustAnchorValid(cfg.ca_pem))) {
    setError("DEVICE-TLS-CA-INVALID");
    return false;
  }
  String transportError;
  String transportBase = cfg.backend_hostport;
  transportBase.trim();
  if (!transportBase.startsWith("http://") && !transportBase.startsWith("https://")) {
    transportBase = "https://" + transportBase;
  }
  if (!inktime::DeviceHttpTransport::backendUrlAllowed(
        transportBase, cfg.ca_pem, transportError)) {
    setError(transportError.length() > 0U ? transportError.c_str() : "DEVICE-URL-INVALID");
    return false;
  }
  const inktime::configstore::ConfigPayload payload = configPayload(cfg);
  String storeError;
  if (!configStore.save(payload, storeError)) {
    if (storeError == "PAIRING-NVS-002") {
      setError("PAIRING-NVS-002");
    } else if (storeError == "PAIRING-NVS-003") {
      setError("PAIRING-NVS-003");
    } else if (storeError == "PAIRING-NVS-004") {
      setError("PAIRING-NVS-004");
    } else if (storeError == "PAIRING-NVS-005") {
      setError("PAIRING-NVS-005");
    } else if (storeError == "PAIRING-NVS-006") {
      setError("PAIRING-NVS-006");
    } else if (storeError == "PAIRING-NVS-007") {
      setError("PAIRING-NVS-007");
    } else {
      setError(storeError.length() > 0U ? storeError.c_str() : "PAIRING-NVS-001");
    }
    return false;
  }
  // Count one successful canonical ConfigStore transaction.  The store owns
  // its internal A/B key writes; this metric intentionally counts transactions
  // rather than pretending to expose driver-level flash operations.
  recordNvsWrite();
  // cfgstore has already completed the formal A/B commit and full read-back.
  // Old dashcfg formal keys are migration-only; stale legacy values must not
  // turn a committed config into a reported failure.
  String legacyReadbackError;
  (void)verifyLegacyConfigReadback(cfg, legacyReadbackError);

#if DEBUG_LOG
  DBG_PRINTLN("[CFG] saved");
#endif
  setError("");
  return true;
}

static String deviceCredential(const Config &cfg) {
  return cfg.device_secret.length() > 0U ? cfg.device_secret : cfg.device_token;
}

static bool hasDeviceCredential(const Config &cfg) {
  return !deviceAuthInvalid && deviceCredential(cfg).length() > 0U;
}

static bool addDeviceAuthorization(HTTPClient &http, const Config &cfg, bool allowInvalid = false) {
  const String credential = deviceCredential(cfg);
  if (credential.length() == 0U || (!allowInvalid && !hasDeviceCredential(cfg))) return false;
  http.addHeader("Authorization", "Bearer " + credential);
  if (cfg.device_secret.length() > 0U && cfg.credential_version > 0U) {
    http.addHeader("X-InkTime-Credential-Version", String(cfg.credential_version));
  }
  return true;
}

static void markDeviceAuthInvalid(Config &cfg, int statusCode) {
  deviceAuthInvalid = true;
  lastDeviceErrorCode = statusCode == 403 ? "DEVICE-AUTH-REVOKED" : "DEVICE-AUTH-INVALID";
  lastDeviceErrorMessage = statusCode == 403
    ? "伺服器撤銷或拒絕裝置認證；需由管理員允許重新配對"
    : "伺服器拒絕裝置認證；保留 credential 並停止本輪網路工作";
  Config candidate = cfg;
  candidate.auth_state = statusCode == 403 ? "revoked" : "auth_invalid";
  String persistError;
  if (!saveConfig(candidate, &persistError)) {
    setConfigPersistenceError(persistError);
    return;
  }
  cfg = candidate;
}

static bool handleDeviceAuthStatus(Config &cfg, int statusCode) {
  if (statusCode != HTTP_CODE_UNAUTHORIZED && statusCode != HTTP_CODE_FORBIDDEN) return false;
  markDeviceAuthInvalid(cfg, statusCode);
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

  html += F("<p><strong>裝置認證：</strong>");
  if (g_cfg.device_token.length() > 0U && g_cfg.device_secret.length() == 0U) {
    html += F("Legacy Token 相容模式（不在此頁顯示或要求重新輸入）。");
  } else if (g_cfg.auth_state == "auth_invalid" || g_cfg.auth_state == "revoked") {
    html += F("認證已失效，請由管理員允許重新配對後再提交設定。");
  } else if (g_cfg.device_secret.length() > 0U) {
    html += F("自動配對已完成；Device Secret 僅保存在裝置 NVS，不會顯示。");
  } else {
    html += F("尚未配對；儲存網路設定後由裝置建立短效配對請求。");
  }
  html += F("</p><br>");

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
  String hourStr  = server.arg("hour");
  String minuteStr = server.arg("minute");
  String tzStr    = server.arg("tz");
  bool rot180Req  = (server.arg("rot180") == "1");

  ssid.trim();
  host.trim();
  caPem.trim();
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
    server.send(400, "text/plain; charset=utf-8", "DEVICE-TLS-CA-INVALID Root CA PEM 格式不合法");
    return;
  }
  if (ssid.length() > 32 || pass.length() > 63 || host.length() > 240
      || host.indexOf('@') >= 0 || !allowedScheme || unsafeOrigin) {
    server.send(400, "text/plain; charset=utf-8", "PAIRING-002 設定格式或長度不合法");
    return;
  }

  Config newCfg = g_cfg;

  if (ssid.length() > 0) newCfg.wifi_ssid = ssid;
  if (pass.length() > 0) newCfg.wifi_pass = pass;

  newCfg.backend_hostport = host;
  if (caProvided) newCfg.ca_pem = caPem;

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

  String transportError;
  if (!inktime::DeviceHttpTransport::backendUrlAllowed(
        newCfg.backend_hostport, newCfg.ca_pem, transportError)) {
    server.send(
      400,
      "text/plain; charset=utf-8",
      String(transportError.length() > 0U ? transportError : "DEVICE-URL-INVALID")
        + " Backend Origin 或 TLS 設定不合法"
    );
    return;
  }

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
#if INKTIME_PHOTOPAINTER_ENABLED
  // This is an intentional configuration restart, not a max-awake recovery.
  inktime::resetMaxAwakeRecoveryState(maxAwakeRecoveryState);
#endif
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
//  Deep Sleep
// =======================
static void goDeepSleepSeconds(uint64_t seconds) {
  if (seconds < 1U) seconds = 1U;
  INK_LOG_INFO("sleep_preparation", "Deep sleep preparation started");

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

  closeWakeHttpSession();
  WiFi.disconnect(false, false);
  WiFi.mode(WIFI_OFF);
  esp_wifi_stop();

#if defined(CONFIG_BT_ENABLED)
  esp_bt_controller_disable();
#endif

  prepareDeepSleepDomains();
  esp_sleep_enable_timer_wakeup(us);

#if INKTIME_PHOTOPAINTER_ENABLED
  // A normal bounded wake reached its sleep boundary. RTC slow memory is also
  // powered down below, but clear explicitly so a future policy change cannot
  // turn one old timeout into a false consecutive-fault sequence.
  inktime::resetMaxAwakeRecoveryState(maxAwakeRecoveryState);
#endif

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
  DBG_PRINTLN("[CFG] provisioning AP started (SSID redacted)");
  DBG_PRINT("[CFG] AP IP   = "); DBG_PRINTLN(WiFi.softAPIP());
#endif

#if INKTIME_PHOTOPAINTER_ENABLED
  if (apOk) {
    const bool pairingScreenReady = photoPainter.displayPairingScreen(
      apSsid.c_str(), apPassword.c_str(), "http://192.168.4.1");
    if (pairingScreenReady) {
      const String refreshMessage = String("Pairing screen refresh completed in ")
          + String(photoPainter.lastRefreshDurationMs()) + String(" ms");
      INK_LOG_INFO("pairing_display_ready", refreshMessage);
    } else {
      INK_LOG_ERROR("pairing_display_failed", photoPainter.lastError());
    }
  }
#endif

  server.on("/", HTTP_GET, handleRoot);
  server.on("/save", HTTP_POST, handleSave);
  server.begin();

  uint32_t enterMs = millis();
#if INKTIME_PHOTOPAINTER_ENABLED
  uint32_t lastPowerCheckMs = enterMs;
  inktime::PowerSourceState portalPowerSource = photoPainter.powerSourceState();
  if (portalPowerSource == inktime::PowerSourceState::Usb) disarmMaxAwakeSupervisor();
#endif

  for (;;) {
    server.handleClient();

#if INKTIME_PHOTOPAINTER_ENABLED
    if (millis() - lastPowerCheckMs >= 5000) {
      photoPainter.refreshPowerState();
      lastPowerCheckMs = millis();
      const inktime::PowerSourceState nextPowerSource = photoPainter.powerSourceState();
      if (portalPowerSource != inktime::PowerSourceState::Usb
          && nextPowerSource == inktime::PowerSourceState::Usb) {
        disarmMaxAwakeSupervisor();
      } else if (portalPowerSource == inktime::PowerSourceState::Usb
                 && nextPowerSource != inktime::PowerSourceState::Usb) {
        armMaxAwakeSupervisor();
      }
      if (portalPowerSource == inktime::PowerSourceState::Usb
          && nextPowerSource == inktime::PowerSourceState::Battery) {
        goDeepSleepMinutes(minutesToNextRefreshFromLastEpoch(g_cfg));
      }
      portalPowerSource = nextPowerSource;
    }
#else
    const bool portalPowerIsUsb = false;
#endif

#if INKTIME_PHOTOPAINTER_ENABLED
    const bool portalPowerIsUsb = portalPowerSource == inktime::PowerSourceState::Usb;
#endif
    if (!portalPowerIsUsb && millis() - enterMs > AP_TIMEOUT_MS) {
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
// Explicit recovery stays long-lived only while USB is positively confirmed.
// Unknown sources remain bounded; battery operation remains one-shot.
bool runUsbServiceMode(bool explicitRecoveryRequested) {
  photoPainter.refreshPowerState();
  // USB power alone is not authorization to alter Wi-Fi or a device token.
  if (!explicitRecoveryRequested) return false;

  inktime::PowerSourceState servicePowerSource = photoPainter.powerSourceState();
  if (servicePowerSource == inktime::PowerSourceState::Battery) return false;
  if (servicePowerSource == inktime::PowerSourceState::Usb) {
    disarmMaxAwakeSupervisor();
  }
  portalSetupSecret = randomPortalSecret();
  portalNonce = randomPortalSecret();
  portalSaveAttempts = 0;
  portalSaveAllowed = true;

  server.on("/", HTTP_GET, handleRoot);
  server.on("/save", HTTP_POST, handleSave);
  server.begin();
#if DEBUG_LOG
  DBG_PRINTLN("[USB] explicit configuration service started");
#endif

  const uint32_t serviceStartedMs = millis();
  uint32_t lastPowerCheckMs = millis();
  for (;;) {
    server.handleClient();
    if (millis() - lastPowerCheckMs >= 5000) {
      photoPainter.refreshPowerState();
      lastPowerCheckMs = millis();
      const inktime::PowerSourceState nextPowerSource = photoPainter.powerSourceState();
      if (servicePowerSource != inktime::PowerSourceState::Usb
          && nextPowerSource == inktime::PowerSourceState::Usb) {
        disarmMaxAwakeSupervisor();
      } else if (servicePowerSource == inktime::PowerSourceState::Usb
                 && nextPowerSource != inktime::PowerSourceState::Usb) {
        armMaxAwakeSupervisor();
      }
      servicePowerSource = nextPowerSource;
    }
    if (servicePowerSource == inktime::PowerSourceState::Battery) break;
    if (servicePowerSource == inktime::PowerSourceState::Unknown
        && millis() - serviceStartedMs > AP_TIMEOUT_MS) break;
    delay(10);
  }
  server.stop();
  armMaxAwakeSupervisor();
  return true;
}
#endif

// =======================
//  WiFi 连接
// =======================
static constexpr uint8_t kWiFiFastPathMaxChannel = 14U;
static constexpr uint32_t kWiFiFastPathTimeoutMs = 3500U;

static bool validBssid(const uint8_t* bssid) {
  if (bssid == nullptr) return false;
  bool allZero = true;
  bool allFf = true;
  for (uint8_t index = 0U; index < 6U; ++index) {
    allZero = allZero && bssid[index] == 0U;
    allFf = allFf && bssid[index] == 0xffU;
  }
  return !allZero && !allFf;
}

static bool loadWiFiFastPathHint(uint8_t* bssid, uint8_t& channel) {
  if (bssid == nullptr) return false;
  prefs.begin("dashcfg", true);
  const size_t length = prefs.getBytesLength("wifi_bssid");
  const size_t read = length == 6U ? prefs.getBytes("wifi_bssid", bssid, 6U) : 0U;
  channel = prefs.getUChar("wifi_channel", 0U);
  prefs.end();
  return read == 6U && channel > 0U && channel <= kWiFiFastPathMaxChannel
      && validBssid(bssid);
}

static void saveWiFiFastPathHint() {
  const uint8_t* bssid = WiFi.BSSID();
  const uint8_t channel = WiFi.channel();
  if (!validBssid(bssid) || channel == 0U || channel > kWiFiFastPathMaxChannel) return;
  prefs.begin("dashcfg", false);
  const size_t written = prefs.putBytes("wifi_bssid", bssid, 6U);
  const size_t channelWritten = prefs.putUChar("wifi_channel", channel);
  prefs.end();
  if (written == 6U) recordNvsWrite();
  if (channelWritten > 0U) recordNvsWrite();
}

static bool waitForWiFi(uint32_t timeout_ms) {
  const uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < timeout_ms) {
    delay(200);
#if DEBUG_LOG
    DBG_PRINT(".");
#endif
  }
  return WiFi.status() == WL_CONNECTED;
}

bool connectWiFi(const Config &cfg, uint32_t timeout_ms = 12000) {
  INK_LOG_DEBUG("wifi_connect_started", "Wi-Fi connection attempt started");
#if DEBUG_LOG
  DBG_PRINTLN("[WIFI] connectWiFi()");
#endif

  const uint32_t started = millis();
  runtimeTelemetry.wifi_fast_path_attempted = false;
  runtimeTelemetry.wifi_fast_path_success = false;
  if (cfg.wifi_ssid.isEmpty()) {
    runtimeTelemetry.wifi_connect_ms = millis() - started;
    return false;
  }

  WiFi.disconnect(false, false);
  WiFi.mode(WIFI_STA);

  WiFi.setSleep(true);
  uint8_t cachedBssid[6] = {0U, 0U, 0U, 0U, 0U, 0U};
  uint8_t cachedChannel = 0U;
  bool ok = false;
  if (loadWiFiFastPathHint(cachedBssid, cachedChannel) && timeout_ms > 0U) {
    runtimeTelemetry.wifi_fast_path_attempted = true;
    const uint32_t fastTimeout = min(timeout_ms, kWiFiFastPathTimeoutMs);
    WiFi.begin(
      cfg.wifi_ssid.c_str(), cfg.wifi_pass.c_str(), cachedChannel, cachedBssid, true);
    ok = waitForWiFi(fastTimeout);
    runtimeTelemetry.wifi_fast_path_success = ok;
    if (!ok) WiFi.disconnect(false, false);
  }
  if (!ok && millis() - started < timeout_ms) {
    WiFi.begin(cfg.wifi_ssid.c_str(), cfg.wifi_pass.c_str());
    ok = waitForWiFi(timeout_ms - (millis() - started));
  }
#if DEBUG_LOG
  DBG_PRINTLN();
#endif

  runtimeTelemetry.wifi_connect_ms = millis() - started;
  if (ok) saveWiFiFastPathHint();
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
static constexpr uint64_t kNtpDueIntervalSeconds = 7ULL * 24ULL * 60ULL * 60ULL;

static bool loadLastNtpEpoch(time_t& epochOut) {
  prefs.begin("dashcfg", true);
  const uint32_t value = prefs.getULong("last_ntp", 0U);
  prefs.end();
  if (value < kPairingMinimumEpoch) return false;
  epochOut = static_cast<time_t>(value);
  return true;
}

static void saveLastNtpEpoch(time_t epoch) {
  if (epoch < static_cast<time_t>(kPairingMinimumEpoch)) return;
  prefs.begin("dashcfg", false);
  const size_t written = prefs.putULong("last_ntp", static_cast<uint32_t>(epoch));
  prefs.end();
  if (written > 0U) recordNvsWrite();
}

static bool localTimeFromSystemClock(struct tm& outLocal) {
  const time_t now = time(nullptr);
  if (now < static_cast<time_t>(kPairingMinimumEpoch)) return false;
  localtime_r(&now, &outLocal);
  return true;
}

static bool seedTimeFromRtc(const Config& cfg, struct tm& outLocal) {
#if INKTIME_PHOTOPAINTER_ENABLED
  time_t rtcEpoch = 0;
  if (photoPainter.readRtc(rtcEpoch) && rtcEpoch >= static_cast<time_t>(kPairingMinimumEpoch)) {
    applyFixedTimezoneWithoutNtp(cfg.tz_offset_minutes);
    struct timeval value = {rtcEpoch, 0};
    settimeofday(&value, nullptr);
    localtime_r(&rtcEpoch, &outLocal);
    return true;
  }
#else
  time_t storedEpoch = 0;
  if (loadLastTimeEpoch(storedEpoch)
      && storedEpoch >= static_cast<time_t>(kPairingMinimumEpoch)) {
    applyFixedTimezoneWithoutNtp(cfg.tz_offset_minutes);
    struct timeval value = {storedEpoch, 0};
    settimeofday(&value, nullptr);
    localtime_r(&storedEpoch, &outLocal);
    return true;
  }
#endif
  return localTimeFromSystemClock(outLocal);
}

static bool ntpDue(bool clockValid) {
  if (!clockValid) return true;
  time_t lastNtp = 0;
  if (!loadLastNtpEpoch(lastNtp)) return true;
  const time_t now = time(nullptr);
  return now < lastNtp || static_cast<uint64_t>(now - lastNtp) >= kNtpDueIntervalSeconds;
}

bool syncTime(const Config &cfg, struct tm &outLocal, bool forceNtp = false) {
#if DEBUG_LOG
  DBG_PRINTLN("[TIME] syncTime start");
#endif
  runtimeTelemetry.ntp_sync_attempted = false;
  runtimeTelemetry.ntp_sync_succeeded = false;
  runtimeTelemetry.ntp_sync_ms = 0;
  const bool seeded = seedTimeFromRtc(cfg, outLocal);
  if (!forceNtp && !ntpDue(seeded)) return seeded;
  runtimeTelemetry.ntp_sync_attempted = true;
  const uint32_t started = millis();
  long offsetSec = (long)cfg.tz_offset_minutes * 60;
  configTime(offsetSec, 0, "pool.ntp.org", "time.nist.gov", "ntp.aliyun.com");

  for (int i = 0; i < 30; ++i) {
    if (getLocalTime(&outLocal)
        && sntp_get_sync_status() == SNTP_SYNC_STATUS_COMPLETED) {
#if DEBUG_LOG
      char buf[64];
      strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &outLocal);
      DBG_PRINT("[TIME] OK: "); DBG_PRINTLN(buf);
#endif
      time_t nowEpoch = time(nullptr);
      if (nowEpoch > 0) {
        saveLastTimeEpoch(nowEpoch);
        saveLastNtpEpoch(nowEpoch);
#if INKTIME_PHOTOPAINTER_ENABLED
        photoPainter.writeRtc(nowEpoch);
#endif
      }
      runtimeTelemetry.ntp_sync_succeeded = true;
      runtimeTelemetry.ntp_sync_ms = millis() - started;
      return true;
    }
    delay(500);
  }
#if DEBUG_LOG
  DBG_PRINTLN("[TIME] syncTime FAILED");
#endif
  runtimeTelemetry.ntp_sync_ms = millis() - started;
#if INKTIME_PHOTOPAINTER_ENABLED
  time_t rtcEpoch = 0;
  if (photoPainter.readRtc(rtcEpoch)
      && rtcEpoch >= static_cast<time_t>(kPairingMinimumEpoch)) {
    applyFixedTimezoneWithoutNtp(cfg.tz_offset_minutes);
    struct timeval value = {rtcEpoch, 0};
    settimeofday(&value, nullptr);
    localtime_r(&rtcEpoch, &outLocal);
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

static bool ensureWakeHttpSession(Config &cfg, String &base) {
  if (WiFi.status() != WL_CONNECTED || !normalizedBackendBase(cfg, base)) return false;
  wakeHttpTransport.configure(cfg.ca_pem);
  if (wakeHttpSessionOpen && wakeHttpOrigin == base && wakeHttpTransport.sessionActive()) {
    return true;
  }
  String transportCode;
  String transportMessage;
  if (!wakeHttpTransport.beginSession(base, transportCode, transportMessage)) {
    wakeHttpSessionOpen = false;
    lastDeviceErrorCode = transportCode.length() ? transportCode : "DEVICE-SESSION-BEGIN";
    lastDeviceErrorMessage = transportMessage.length()
      ? transportMessage : "Wake HTTP session 初始化失敗";
    return false;
  }
  wakeHttpOrigin = base;
  wakeHttpSessionOpen = true;
  return true;
}

static void closeWakeHttpSession() {
  wakeHttpTransport.closeSession();
  wakeHttpOrigin = "";
  wakeHttpSessionOpen = false;
}

static void stopNetworkBeforeDisplay() {
  closeWakeHttpSession();
  WiFi.disconnect(false, false);
  WiFi.mode(WIFI_OFF);
  esp_wifi_stop();
  networkClosedForDisplay = true;
}

static constexpr uint32_t kPairingPollWindowMs = 30000U;
static constexpr uint32_t kPairingPollDelayMs = 3000U;

static uint64_t pairingNowEpoch() {
  time_t now = time(nullptr);
  if (now >= static_cast<time_t>(kPairingMinimumEpoch)) return static_cast<uint64_t>(now);
  if (loadLastTimeEpoch(now) && now >= static_cast<time_t>(kPairingMinimumEpoch)) {
    return static_cast<uint64_t>(now);
  }
  return 0U;
}

static uint64_t pairingRetryNowEpoch() {
  const time_t now = time(nullptr);
  return now >= static_cast<time_t>(kPairingMinimumEpoch)
    ? static_cast<uint64_t>(now) : 0U;
}

static uint32_t pairingBackoffForAttempt(uint8_t attempt) {
  return inktime::pairing::backoffSeconds(attempt);
}

static bool pairingRetryDue(const Config &cfg) {
  return inktime::pairing::retryDue(
    retryStateFromConfig(cfg), pairingRetryNowEpoch());
}

static uint32_t pairingBackoffSeconds(const Config &cfg) {
  return inktime::pairing::sleepSeconds(
    retryStateFromConfig(cfg), pairingRetryNowEpoch());
}

static bool pairingExpiryPassed(const Config &cfg) {
  const uint64_t now = pairingNowEpoch();
  return cfg.pairing_expires_at_epoch != 0U
    && now != 0U
    && now >= cfg.pairing_expires_at_epoch;
}

static bool savePairingCandidate(Config &cfg, const Config &candidate) {
  String persistError;
  if (!saveConfig(candidate, &persistError)) {
    setConfigPersistenceError(persistError);
    return false;
  }
  cfg = candidate;
  return true;
}

static bool persistPairingRetry(Config &cfg, const String &state) {
  Config candidate = cfg;
  candidate.auth_state = state;
  const uint8_t previousAttempt = candidate.pairing_retry_attempt;
  candidate.pairing_retry_attempt = previousAttempt < 8U
    ? static_cast<uint8_t>(previousAttempt + 1U) : 8U;
  const uint64_t now = pairingNowEpoch();
  candidate.pairing_retry_at_epoch = now == 0U
    ? 0U : now + pairingBackoffForAttempt(previousAttempt);
  return savePairingCandidate(cfg, candidate);
}

static bool persistRepairPermissionRetry(Config &cfg) {
  PairingRetryMetadataStore retryStore;
  const inktime::pairing::RetryState current = retryStateFromConfig(cfg);
  const inktime::pairing::RetryState next = inktime::pairing::nextRetryState(
    current, pairingRetryNowEpoch());
  applyRetryStateToConfig(next, cfg);
  if (!(next == current)) {
    String error;
    if (!retryStore.save(next, error)) {
      setConfigPersistenceError(error);
      return false;
    }
  }
  return true;
}

static bool clearRepairPermissionRetry() {
  PairingRetryMetadataStore retryStore;
  String error;
  if (!retryStore.clear(error)) {
    setConfigPersistenceError(error);
    return false;
  }
  return true;
}

static bool persistPairingExpired(Config &cfg) {
  Config candidate = cfg;
  // An unconfirmed envelope is no longer usable after expiry.  Clear its
  // locally persisted material so a fresh enrollment can proceed.  If this
  // was a repair flow for an existing device, the backend's
  // repair_allowed_until gate still requires a new explicit admin permission
  // before the next request can succeed.
  candidate.auth_state = "pairing_expired";
  candidate.credential_version = 0U;
  candidate.device_secret = "";
  candidate.pairing_id = "";
  candidate.pairing_nonce = "";
  candidate.pairing_expires_at_epoch = 0U;
  const uint8_t previousAttempt = candidate.pairing_retry_attempt;
  candidate.pairing_retry_attempt = previousAttempt < 8U
    ? static_cast<uint8_t>(previousAttempt + 1U) : 8U;
  const uint64_t now = pairingNowEpoch();
  candidate.pairing_retry_at_epoch = now == 0U
    ? 0U : now + pairingBackoffForAttempt(previousAttempt);
  return savePairingCandidate(cfg, candidate);
}

static bool automaticPairingAllowed(const Config &cfg) {
#if INKTIME_PHOTOPAINTER_ENABLED
  if (cfg.delivery_mode == "stock_compat") return false;
#endif
  if (cfg.auth_state == "credential_issued") {
    return cfg.device_secret.length() > 0U
      && cfg.pairing_id.length() > 0U
      && cfg.pairing_nonce.length() > 0U
      && pairingRetryDue(cfg);
  }
  if (cfg.auth_state != "unpaired" && cfg.auth_state != "pairing_pending"
      && cfg.auth_state != "pairing_expired") return false;
  const bool credentialCanBeIssued = cfg.device_secret.length() == 0U
    && cfg.device_token.length() == 0U;
  if (cfg.auth_state != "pairing_pending" && !credentialCanBeIssued) return false;
  return pairingRetryDue(cfg);
}

static bool checkRepairPermission(Config &cfg) {
  if (cfg.auth_state != "auth_invalid" && cfg.auth_state != "revoked") return false;
  if (deviceCredential(cfg).length() == 0U) {
    lastDeviceErrorCode = "DEVICE-PAIRING-PERMISSION-CREDENTIAL";
    lastDeviceErrorMessage = "重新配對 permission 缺少現有 credential；本輪停止網路工作";
    if (!persistRepairPermissionRetry(cfg)) return false;
    return false;
  }
  String base;
  if (!normalizedBackendBase(cfg, base)) {
    if (!persistRepairPermissionRetry(cfg)) return false;
    return false;
  }

  inktime::DeviceHttpTransport transport(cfg.ca_pem);
  HTTPClient http;
  String transportCode;
  String transportMessage;
  if (!transport.begin(
        http, base + String(DEVICE_PAIRING_REPAIR_PERMISSION_PATH), 15000,
        transportCode, transportMessage)) {
    lastDeviceErrorCode = transportCode.length() ? transportCode : "DEVICE-PAIRING-PERMISSION-URL";
    lastDeviceErrorMessage = transportMessage.length() ? transportMessage : "重新配對 permission URL 無法初始化";
    if (!persistRepairPermissionRetry(cfg)) return false;
    return false;
  }
  if (!addDeviceAuthorization(http, cfg, true)) {
    http.end();
    if (!persistRepairPermissionRetry(cfg)) return false;
    return false;
  }
  const char* headers[] = {"Content-Type"};
  http.collectHeaders(headers, 1);
  const int status = countedHttpGet(http);
  const int length = http.getSize();
  const String contentType = http.header("Content-Type");
  if (status != HTTP_CODE_OK || length <= 0 || length > 2048
      || !contentType.startsWith("application/json")) {
    http.end();
    lastDeviceErrorCode = "DEVICE-PAIRING-PERMISSION";
    lastDeviceErrorMessage = "管理員尚未允許重新配對；本輪不建立 pairing request";
    if (!persistRepairPermissionRetry(cfg)) return false;
    return false;
  }
  JsonDocument response;
  const DeserializationError jsonError = deserializeJson(response, http.getStream());
  http.end();
  const String statusValue = response["status"] | "";
  const String authorizedDeviceId = response["device_id"] | "";
  if (jsonError || response.overflowed() || statusValue != "pairing_allowed"
      || authorizedDeviceId != cfg.device_id) {
    lastDeviceErrorCode = "DEVICE-PAIRING-PERMISSION-SCHEMA";
    lastDeviceErrorMessage = "重新配對 permission response schema 不合法";
    if (!persistRepairPermissionRetry(cfg)) return false;
    return false;
  }
  Config candidate = cfg;
  candidate.auth_state = "pairing_pending";
  candidate.pairing_id = "";
  candidate.pairing_nonce = "";
  candidate.pairing_expires_at_epoch = 0U;
  candidate.pairing_retry_at_epoch = 0U;
  candidate.pairing_retry_attempt = 0U;
  if (!savePairingCandidate(cfg, candidate)) return false;
  return clearRepairPermissionRetry();
}

static bool confirmPairingCredential(Config &cfg, const String &base) {
  if (cfg.device_secret.length() == 0U || cfg.credential_version == 0U
      || cfg.device_id.length() == 0U || cfg.pairing_id.length() == 0U
      || cfg.pairing_nonce.length() == 0U) return false;
  JsonDocument confirmRequest;
  confirmRequest["pairing_id"] = cfg.pairing_id;
  confirmRequest["device_id"] = cfg.device_id;
  confirmRequest["pairing_nonce"] = cfg.pairing_nonce;
  String confirmBody;
  serializeJson(confirmRequest, confirmBody);

  inktime::DeviceHttpTransport transport(cfg.ca_pem);
  HTTPClient http;
  String transportCode;
  String transportMessage;
  if (!transport.begin(
        http, base + String(DEVICE_PAIRING_CONFIRM_PATH), 15000,
        transportCode, transportMessage)) {
    (void)persistPairingRetry(cfg, "credential_issued");
    lastDeviceErrorCode = transportCode.length() ? transportCode : "DEVICE-PAIRING-CONFIRM-URL";
    lastDeviceErrorMessage = transportMessage.length() ? transportMessage : "配對 confirm URL 無法初始化";
    return false;
  }
  const char* headers[] = {"Content-Type"};
  http.collectHeaders(headers, 1);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", "Bearer " + cfg.device_secret);
  http.addHeader("X-InkTime-Credential-Version", String(cfg.credential_version));
  const int status = countedHttpPost(http, confirmBody);
  const int length = http.getSize();
  const String contentType = http.header("Content-Type");
  if (status == HTTP_CODE_GONE) {
    http.end();
    (void)persistPairingExpired(cfg);
    lastDeviceErrorCode = "DEVICE-PAIRING-EXPIRED";
    lastDeviceErrorMessage = "credential envelope 已過期；等待管理員明確允許 repair";
    return false;
  }
  if (status != HTTP_CODE_OK || length <= 0 || length > 4096
      || !contentType.startsWith("application/json")) {
    http.end();
    (void)persistPairingRetry(cfg, "credential_issued");
    lastDeviceErrorCode = "DEVICE-PAIRING-CONFIRM";
    lastDeviceErrorMessage = "配對 credential 尚未完成 confirm；保留暫存 credential 供下次恢復";
    return false;
  }
  JsonDocument response;
  const DeserializationError jsonError = deserializeJson(response, http.getStream());
  http.end();
  const String statusValue = response["status"] | "";
  const String confirmedDeviceId = response["device_id"] | "";
  if (jsonError || response.overflowed()
      || (statusValue != "confirmed" && statusValue != "already_confirmed")
      || confirmedDeviceId != cfg.device_id) {
    (void)persistPairingRetry(cfg, "credential_issued");
    lastDeviceErrorCode = "DEVICE-PAIRING-CONFIRM-SCHEMA";
    lastDeviceErrorMessage = "配對 confirm response schema 不合法；保留暫存 credential";
    return false;
  }
  Config candidate = cfg;
  candidate.auth_state = "paired";
  candidate.pairing_id = "";
  candidate.pairing_nonce = "";
  candidate.pairing_expires_at_epoch = 0U;
  candidate.pairing_retry_at_epoch = 0U;
  candidate.pairing_retry_attempt = 0U;
  if (!savePairingCandidate(cfg, candidate)) return false;
  deviceAuthInvalid = false;
  lastDeviceErrorCode = "";
  lastDeviceErrorMessage = "";
  return true;
}

static bool claimPairingCredential(Config &cfg, const String &base) {
  if (cfg.pairing_id.length() == 0U || cfg.pairing_nonce.length() == 0U
      || pairingExpiryPassed(cfg)) {
    (void)persistPairingExpired(cfg);
    lastDeviceErrorCode = "DEVICE-PAIRING-EXPIRED";
    lastDeviceErrorMessage = "配對請求已過期；等待 bounded backoff 後重新建立";
    return false;
  }
  const uint32_t pollStarted = millis();
  while (static_cast<uint32_t>(millis() - pollStarted) < kPairingPollWindowMs) {
    JsonDocument claimRequest;
    claimRequest["pairing_id"] = cfg.pairing_id;
    claimRequest["pairing_nonce"] = cfg.pairing_nonce;
    String claimBody;
    serializeJson(claimRequest, claimBody);
    inktime::DeviceHttpTransport transport(cfg.ca_pem);
    HTTPClient http;
    String transportCode;
    String transportMessage;
    if (!transport.begin(
          http, base + String(DEVICE_PAIRING_CLAIM_PATH), 15000,
          transportCode, transportMessage)) {
      (void)persistPairingRetry(cfg, "pairing_pending");
      lastDeviceErrorCode = transportCode.length() ? transportCode : "DEVICE-PAIRING-CLAIM-URL";
      lastDeviceErrorMessage = transportMessage.length() ? transportMessage : "配對 claim URL 無法初始化";
      return false;
    }
    const char* headers[] = {"Content-Type"};
    http.collectHeaders(headers, 1);
    http.addHeader("Content-Type", "application/json");
    const int status = countedHttpPost(http, claimBody);
    const int length = http.getSize();
    const String contentType = http.header("Content-Type");
    if (status == HTTP_CODE_ACCEPTED) {
      http.end();
      delay(kPairingPollDelayMs);
      continue;
    }
    if (status == HTTP_CODE_GONE) {
      http.end();
      (void)persistPairingExpired(cfg);
      lastDeviceErrorCode = "DEVICE-PAIRING-EXPIRED";
      lastDeviceErrorMessage = "配對請求已過期；等待 bounded backoff 後重新建立";
      return false;
    }
    if (status != HTTP_CODE_OK || length <= 0 || length > 8192
        || !contentType.startsWith("application/json")) {
      http.end();
      (void)persistPairingRetry(cfg, "pairing_pending");
      lastDeviceErrorCode = "DEVICE-PAIRING-CLAIM";
      lastDeviceErrorMessage = "配對 claim HTTP／Content-Type／長度不合法；保留 pairing state";
      return false;
    }
    JsonDocument response;
    const DeserializationError jsonError = deserializeJson(response, http.getStream());
    http.end();
    const String secret = response["device_secret"] | "";
    const JsonVariantConst versionValue = response["credential_version"];
    const String claimedDeviceId = response["device_id"] | "";
    if (jsonError || response.overflowed() || secret.length() < 32U
        || secret.length() > inktime::configstore::kMaxDeviceSecretBytes
        || !versionValue.is<uint32_t>() || versionValue.is<bool>() || versionValue.as<uint32_t>() == 0U
        || claimedDeviceId != cfg.device_id) {
      (void)persistPairingRetry(cfg, "pairing_pending");
      lastDeviceErrorCode = "DEVICE-PAIRING-SCHEMA";
      lastDeviceErrorMessage = "配對 claim credential schema 不合法；保留 pairing state";
      return false;
    }
    Config candidate = cfg;
    candidate.device_secret = secret;
    candidate.credential_version = versionValue.as<uint32_t>();
    candidate.auth_state = "credential_issued";
#if INKTIME_PHOTOPAINTER_ENABLED
    JsonObject claimConfig = response["config"].as<JsonObject>();
    const String delivery = claimConfig["delivery_mode"] | candidate.delivery_mode;
    const String button = claimConfig["button_wake_action"] | candidate.button_wake_action;
    if (validDeliveryMode(delivery)) candidate.delivery_mode = delivery;
    if (validButtonWakeAction(button)) candidate.button_wake_action = button;
#endif
    candidate.pairing_retry_at_epoch = 0U;
    candidate.pairing_retry_attempt = 0U;
    if (!savePairingCandidate(cfg, candidate)) return false;
    return confirmPairingCredential(cfg, base);
  }
  (void)persistPairingRetry(cfg, "pairing_pending");
  lastDeviceErrorCode = "DEVICE-PAIRING-TIMEOUT";
  lastDeviceErrorMessage = "管理員核准尚未完成；保留 pairing state 並等待 bounded backoff";
  return false;
}

static bool performAutomaticPairing(Config &cfg) {
  if (!automaticPairingAllowed(cfg)) {
    return cfg.auth_state == "paired" && deviceCredential(cfg).length() > 0U && !deviceAuthInvalid;
  }
  String base;
  if (!normalizedBackendBase(cfg, base)) return false;

  if (cfg.auth_state == "credential_issued") {
    return confirmPairingCredential(cfg, base);
  }
  if (cfg.auth_state == "pairing_pending" && cfg.pairing_id.length() > 0U
      && cfg.pairing_nonce.length() > 0U) {
    return claimPairingCredential(cfg, base);
  }

  Config identityCandidate = cfg;
  if (identityCandidate.device_id.length() == 0U) {
    identityCandidate.device_id = String("esp32-") + randomPortalSecret();
    if (!savePairingCandidate(cfg, identityCandidate)) return false;
  }
  const String pairingNonce = cfg.pairing_nonce.length() > 0U
    ? cfg.pairing_nonce : randomPortalSecret();
  // Commit the nonce before the network request.  If the request reaches the
  // server but its response is lost during a power cut, the next wake can
  // repeat the same nonce and recover the original enrollment instead of
  // creating a second identity or getting stuck on a nonce conflict.
  Config requestCandidate = cfg;
  requestCandidate.auth_state = "pairing_pending";
  requestCandidate.pairing_id = "";
  requestCandidate.pairing_nonce = pairingNonce;
  requestCandidate.pairing_expires_at_epoch = 0U;
  requestCandidate.pairing_retry_at_epoch = 0U;
  if (!savePairingCandidate(cfg, requestCandidate)) return false;
  JsonDocument pairingRequest;
  pairingRequest["device_id"] = cfg.device_id;
  pairingRequest["pairing_nonce"] = pairingNonce;
  pairingRequest["firmware_identity"] = kBoardConfig.name;
  pairingRequest["firmware_version"] = INKTIME_FIRMWARE_VERSION;
  pairingRequest["panel_profile"] = INKTIME_PANEL_PROFILE;
  JsonObject capabilities = pairingRequest["capabilities"].to<JsonObject>();
  capabilities["automatic_pairing"] = true;
  capabilities["ab_credential_store"] = true;
  capabilities["offline_schedule_max_slots"] = 24;
  capabilities["stock_compatibility"] = true;
  capabilities["deep_sleep"] = true;
  String requestBody;
  serializeJson(pairingRequest, requestBody);

  inktime::DeviceHttpTransport requestTransport(cfg.ca_pem);
  HTTPClient requestHttp;
  String transportCode;
  String transportMessage;
  if (!requestTransport.begin(
        requestHttp, base + String(DEVICE_PAIRING_REQUEST_PATH), 15000,
        transportCode, transportMessage)) {
    (void)persistPairingRetry(cfg, "pairing_pending");
    lastDeviceErrorCode = transportCode.length() ? transportCode : "DEVICE-PAIRING-URL";
    lastDeviceErrorMessage = transportMessage.length() ? transportMessage : "配對 request URL 無法初始化";
    return false;
  }
  const char* pairingHeaders[] = {"Content-Type"};
  requestHttp.collectHeaders(pairingHeaders, 1);
  requestHttp.addHeader("Content-Type", "application/json");
  const int requestStatus = countedHttpPost(requestHttp, requestBody);
  const int requestLength = requestHttp.getSize();
  const String requestContentType = requestHttp.header("Content-Type");
  if ((requestStatus != HTTP_CODE_CREATED && requestStatus != HTTP_CODE_OK)
      || requestLength <= 0 || requestLength > 8192
      || !requestContentType.startsWith("application/json")) {
    requestHttp.end();
    (void)persistPairingRetry(cfg, "pairing_pending");
    lastDeviceErrorCode = "DEVICE-PAIRING-REQUEST";
    lastDeviceErrorMessage = "配對 request HTTP／Content-Type／長度不合法";
    return false;
  }
  JsonDocument requestResponse;
  const DeserializationError requestJsonError = deserializeJson(requestResponse, requestHttp.getStream());
  requestHttp.end();
  const String pairingId = requestResponse["pairing_id"] | "";
  const String pairingCode = requestResponse["pairing_code"] | "";
  const String requestState = requestResponse["status"] | "pending";
  const bool requestReused = requestResponse["request_reused"] | false;
  const JsonVariantConst expiresValue = requestResponse["expires_in_seconds"];
  const JsonVariantConst serverEpochValue = requestResponse["server_epoch"];
  uint64_t serverEpoch = serverEpochValue.is<uint64_t>()
    ? serverEpochValue.as<uint64_t>() : pairingNowEpoch();
  if (serverEpoch < kPairingMinimumEpoch) serverEpoch = pairingNowEpoch();
  const bool validCode = pairingCode.length() == 6U
    && pairingCode[0] >= '0' && pairingCode[0] <= '9'
    && pairingCode[1] >= '0' && pairingCode[1] <= '9'
    && pairingCode[2] >= '0' && pairingCode[2] <= '9'
    && pairingCode[3] >= '0' && pairingCode[3] <= '9'
    && pairingCode[4] >= '0' && pairingCode[4] <= '9'
    && pairingCode[5] >= '0' && pairingCode[5] <= '9';
  const bool requestPending = requestState == "pending";
  const bool requestClaimable = requestState == "approved" || requestState == "credential_issued";
  if (requestJsonError || requestResponse.overflowed() || pairingId.length() == 0U
      || !expiresValue.is<int32_t>() || expiresValue.is<bool>()
      || expiresValue.as<int32_t>() < 1 || expiresValue.as<int32_t>() > 300
      || serverEpoch < kPairingMinimumEpoch
      || (!requestPending && !requestClaimable)
      || (requestPending && !validCode)
      || (requestReused && cfg.pairing_nonce != pairingNonce)) {
    (void)persistPairingRetry(cfg, "pairing_pending");
    lastDeviceErrorCode = "DEVICE-PAIRING-SCHEMA";
    lastDeviceErrorMessage = "配對 request 回應 schema 不合法；不建立新的裝置 credential";
    return false;
  }
  Config candidate = cfg;
  candidate.pairing_id = pairingId;
  candidate.pairing_nonce = requestReused ? cfg.pairing_nonce : pairingNonce;
  candidate.pairing_expires_at_epoch = serverEpoch + static_cast<uint64_t>(expiresValue.as<int32_t>());
  candidate.pairing_retry_at_epoch = 0U;
  candidate.pairing_retry_attempt = 0U;
  candidate.auth_state = "pairing_pending";
  if (!savePairingCandidate(cfg, candidate)) return false;
#if INKTIME_PHOTOPAINTER_ENABLED
  if (requestPending && validCode) {
    (void)photoPainter.displayPairingScreen(
      cfg.wifi_ssid.c_str(), "", base.c_str(), pairingCode.c_str());
  }
#else
  if (requestPending && validCode) (void)displayPairingCode(cfg, pairingCode);
#endif
  return claimPairingCredential(cfg, base);
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

static PendingQueueAck ramQueueAckBatch[kQueueAckBatchMaxEvents] = {};
static uint8_t ramQueueAckBatchCount = 0U;

static bool terminalQueueAck(inktime::QueueEvent event) {
  return event == inktime::QueueEvent::DisplayCompleted
    || event == inktime::QueueEvent::DisplayFailed;
}

static bool persistQueueAckBatch(
  const PendingQueueAck *pending,
  uint8_t count
) {
  if (pending == nullptr || count == 0U) return false;
  bool ok = true;
  for (uint8_t index = 0U; index < count; ++index) {
    ok = inktime::ackjournal::allPersistenceSucceeded(
      ok, persistPendingQueueAck(pending[index]));
  }
  if (!ok) {
    lastDeviceErrorCode = "DEVICE-QUEUE-ACK-DURABILITY";
    lastDeviceErrorMessage = "Queue ACK pending persistence failed; no durable-retention claim";
  }
  return ok;
}

static bool serializeQueueAckBatch(
  const PendingQueueAck *pending,
  uint8_t count,
  String &body
) {
  if (pending == nullptr || count == 0U || count > kQueueAckBatchMaxEvents) return false;
  JsonDocument payload;
  JsonArray events = payload["events"].to<JsonArray>();
  for (uint8_t index = 0U; index < count; ++index) {
    if (!pending[index].valid) return false;
    String idempotencyKey;
    if (!queueAckIdempotencyKey(pending[index], idempotencyKey)) return false;
    JsonObject event = events.add<JsonObject>();
    event["queue_item_id"] = pending[index].queueItemId;
    event["queue_version"] = pending[index].queueVersion;
    event["event"] = inktime::queueEventName(pending[index].event);
    event["idempotency_key"] = idempotencyKey;
    if (pending[index].displaySkipped) {
      event["display_skipped"] = true;
      event["skip_reason"] = "same_sha256";
    }
    if (pending[index].delayedTerminal) {
      event["ack_mode"] = "delayed_terminal";
      event["release_id"] = pending[index].releaseId;
      if (pending[index].eventEpoch > 0) event["event_epoch"] = pending[index].eventEpoch;
    }
    if (pending[index].errorCode.length() > 0U) event["error_code"] = pending[index].errorCode;
  }
  body = "";
  serializeJson(payload, body);
  return !payload.overflowed() && body.length() > 0U
    && body.length() <= kQueueAckBatchMaxBodyBytes;
}

static bool postQueueAckBatch(
  Config &cfg,
  const PendingQueueAck *pending,
  uint8_t count,
  bool allowRetainedTerminal = false
) {
  if (pending == nullptr || count == 0U) return true;
  queueAckPermanentReject = false;
  if (deviceAuthInvalid) {
    const bool retained = persistQueueAckBatch(pending, count);
    if (!retained) lastDeviceWarningMessage = "device auth invalid 且 pending ACK 未能 durable 保留";
    return false;
  }
  String body;
  if (!serializeQueueAckBatch(pending, count, body)) {
    const bool retained = persistQueueAckBatch(pending, count);
    if (!retained) lastDeviceWarningMessage = "ACK schema failure 且 pending ACK 未能 durable 保留";
    lastDeviceErrorCode = "DEVICE-QUEUE-ACK-SCHEMA";
    lastDeviceErrorMessage = "Queue ACK batch body 超過有界 schema 或 idempotency 建立失敗";
    return false;
  }

  String base;
  if (!ensureWakeHttpSession(cfg, base) || WiFi.status() != WL_CONNECTED) {
    const bool retained = persistQueueAckBatch(pending, count);
    if (!retained) lastDeviceWarningMessage = "network unavailable 且 pending ACK 未能 durable 保留";
    return false;
  }

  for (uint8_t attempt = 0U; attempt <= inktime::kQueueRetryLimit; ++attempt) {
    HTTPClient ackHttp;
    String transportCode;
    String transportMessage;
    if (!wakeHttpTransport.begin(
          ackHttp, base + String(DEVICE_QUEUE_ACK_BATCH_PATH), 15000,
          transportCode, transportMessage)) {
      lastDeviceErrorCode = transportCode;
      lastDeviceErrorMessage = transportMessage;
      break;
    }
    if (!addDeviceAuthorization(ackHttp, cfg)) {
      ackHttp.end();
      const bool retained = persistQueueAckBatch(pending, count);
      if (!retained) lastDeviceWarningMessage = "authorization unavailable 且 pending ACK 未能 durable 保留";
      return false;
    }
    const char* responseHeaders[] = {"Content-Type"};
    ackHttp.collectHeaders(responseHeaders, 1);
    ackHttp.addHeader("Content-Type", "application/json");
    if (runtimeTelemetry.ack_batch_request_count < UINT32_MAX) {
      ++runtimeTelemetry.ack_batch_request_count;
    }
    const int status = countedHttpPost(ackHttp, body);
    const bool authFailed = handleDeviceAuthStatus(cfg, status);
    const int responseLength = ackHttp.getSize();
    const String responseContentType = ackHttp.header("Content-Type");
    if (authFailed) {
      ackHttp.end();
      const bool retained = persistQueueAckBatch(pending, count);
      if (!retained) lastDeviceWarningMessage = "authorization failure 且 pending ACK 未能 durable 保留";
      return false;
    }

    if (status == HTTP_CODE_OK && responseLength > 0 && responseLength <= 12 * 1024
        && responseContentType.startsWith("application/json")) {
      JsonDocument response;
      const DeserializationError responseError = deserializeJson(response, ackHttp.getStream());
      const String responseStatus = response["status"] | "";
      const JsonArrayConst results = response["results"].as<JsonArrayConst>();
      const bool responseValid = !responseError && !response.overflowed()
        && responseStatus == "ok" && !results.isNull() && results.size() == count;
      if (responseValid) {
        bool allResolved = true;
        bool stale = false;
        bool retainedTerminal = false;
        bool unresolvedOther = false;
        bool permanentRejected = false;
        bool durabilityFailure = false;
        for (uint8_t index = 0U; index < count; ++index) {
          const JsonObjectConst result = results[index].as<JsonObjectConst>();
          const String responseItem = result["queue_item_id"] | "";
          const String responseEvent = result["event"] | "";
          const int responseHttpStatus = result["http_status"] | 0;
          const String outcome = result["status"] | "";
          const String errorCode = result["error_code"] | "";
          const inktime::QueueAckResultDisposition disposition =
            inktime::queueAckResultDisposition(
              pending[index].queueItemId.c_str(),
              inktime::queueEventName(pending[index].event),
              responseItem.c_str(),
              responseEvent.c_str(),
              responseHttpStatus,
              outcome.c_str(),
              errorCode.c_str());
          if (disposition == inktime::QueueAckResultDisposition::Accepted) {
            const bool removed = removePendingQueueAck(pending[index]);
            if (inktime::ackjournal::retainDuplicateEvidence(true, removed)) {
              lastDeviceWarningCode = "DEVICE-QUEUE-ACK-CLEANUP";
              lastDeviceWarningMessage = "server 已接受 ACK，但 local cleanup 失敗；保留 duplicate-safe evidence";
            }
            continue;
          }
          if (disposition == inktime::QueueAckResultDisposition::Stale) {
            // Stale evidence is retained for recovery, but it can never
            // authorize this wake's display/config progression.
            allResolved = false;
            // A terminal event is crash-safe evidence, even when the queue
            // version has moved on before the next wake.  Keep it durable so
            // delayed-terminal recovery can still be accepted; dropping it
            // here would turn a transient stale response into lost history.
            if (terminalQueueAck(pending[index].event)) {
              if (!persistPendingQueueAck(pending[index])) {
                unresolvedOther = true;
                durabilityFailure = true;
              }
              allResolved = false;
              stale = true;
              retainedTerminal = true;
              continue;
            }
            const bool removed = removePendingQueueAck(pending[index]);
            if (inktime::ackjournal::retainDuplicateEvidence(true, removed)) {
              lastDeviceWarningCode = "DEVICE-QUEUE-ACK-CLEANUP";
              lastDeviceWarningMessage = "QUEUE-003 rejected ACK cleanup 失敗；保留 duplicate-safe evidence";
            }
            stale = true;
            continue;
          }
          if (disposition == inktime::QueueAckResultDisposition::AuthoritativePermanentReject) {
            // A per-event 4xx result is authoritative for this item.  Do not
            // keep retrying it or strand later config/schedule work in this
            // wake; quarantine the event and expose the rejection.
            const bool removed = removePendingQueueAck(pending[index]);
            if (inktime::ackjournal::retainDuplicateEvidence(true, removed)) {
              lastDeviceWarningCode = "DEVICE-QUEUE-ACK-CLEANUP";
              lastDeviceWarningMessage = "permanent ACK cleanup 失敗；保留 duplicate-safe evidence";
            }
            permanentRejected = true;
            continue;
          }
          if (!persistPendingQueueAck(pending[index])) {
            lastDeviceErrorCode = "DEVICE-QUEUE-ACK-DURABILITY";
            lastDeviceErrorMessage = "Queue ACK rejected event 無法 durable 保留";
            durabilityFailure = true;
          }
          allResolved = false;
          unresolvedOther = true;
        }
        ackHttp.end();
        queueAckPermanentReject = inktime::queueAckMayUnlockDisplay(
          permanentRejected, allResolved, unresolvedOther, durabilityFailure);
        if (stale) {
          lastDeviceErrorCode = "DEVICE-QUEUE-STALE";
          lastDeviceErrorMessage = "QUEUE-003 Queue version 已過期；下次重新取得 Manifest";
        }
        if (permanentRejected) {
          lastDeviceErrorCode = "DEVICE-QUEUE-ACK-PERMANENT";
          lastDeviceErrorMessage = "Queue ACK 已被伺服器永久拒絕；事件已 quarantine，本輪繼續後續工作";
        }
        if (allResolved) return true;
        if (allowRetainedTerminal && retainedTerminal && !unresolvedOther) return true;
        if (durabilityFailure) {
          lastDeviceErrorCode = "DEVICE-QUEUE-ACK-DURABILITY";
          lastDeviceErrorMessage = "Queue ACK batch rejected events 未能 durable 保留";
        } else {
          lastDeviceErrorCode = stale ? lastDeviceErrorCode : "DEVICE-QUEUE-ACK-REJECTED";
          lastDeviceErrorMessage = stale
            ? lastDeviceErrorMessage
            : "Queue ACK batch 含有拒絕事件；拒絕項目已 durable 保留";
        }
        return false;
      }
    }

    ackHttp.end();
    const inktime::AckDecision decision = inktime::ackDecision(status, attempt);
    if (decision == inktime::AckDecision::StaleManifest) {
      const bool retained = persistQueueAckBatch(pending, count);
      lastDeviceErrorCode = "DEVICE-QUEUE-STALE";
      lastDeviceErrorMessage = retained
        ? "Queue ACK batch HTTP 409；pending events 已 durable 保留"
        : "Queue ACK batch HTTP 409；pending events 未能 durable 保留";
      return false;
    }
    if (decision == inktime::AckDecision::AuthorizationFailed) {
      const bool retained = persistQueueAckBatch(pending, count);
      lastDeviceErrorCode = "DEVICE-QUEUE-AUTH";
      lastDeviceErrorMessage = retained
        ? "Queue ACK Token／authorization 被拒絕；pending events 已 durable 保留"
        : "Queue ACK Token／authorization 被拒絕；pending events 未能 durable 保留";
      return false;
    }
    if (status == 429) {
      // Rate limiting is transient.  Keep the complete batch durable even
      // after the bounded retry budget is exhausted; it must never enter the
      // permanent-4xx quarantine path below.
      if (decision == inktime::AckDecision::Retry) {
        delay(250U * (attempt + 1U));
        continue;
      }
      break;
    }
    if (status >= 400 && status < 500 && status != HTTP_CODE_CONFLICT) {
      queueAckPermanentReject = true;
      for (uint8_t index = 0U; index < count; ++index) {
        const bool removed = removePendingQueueAck(pending[index]);
        if (inktime::ackjournal::retainDuplicateEvidence(true, removed)) {
          lastDeviceWarningCode = "DEVICE-QUEUE-ACK-CLEANUP";
          lastDeviceWarningMessage = "permanent HTTP ACK cleanup 失敗；保留 duplicate-safe evidence";
        }
      }
      lastDeviceErrorCode = "DEVICE-QUEUE-ACK-PERMANENT";
      lastDeviceErrorMessage = "Queue ACK HTTP 4xx 已永久拒絕；事件已 quarantine，本輪繼續後續工作";
      return true;
    }
    if (decision != inktime::AckDecision::Retry) break;
    delay(250U * (attempt + 1U));
  }
  const bool retained = persistQueueAckBatch(pending, count);
  lastDeviceErrorCode = "DEVICE-QUEUE-ACK-RETRY";
  lastDeviceErrorMessage = retained
    ? "Queue ACK batch 已達有界 retry 上限；pending events 已 durable 保留"
    : "Queue ACK batch 已達有界 retry 上限；pending events 未能 durable 保留";
  return false;
}

static bool flushRamQueueAckBatch(Config &cfg) {
  if (ramQueueAckBatchCount == 0U) return true;
  PendingQueueAck pending[kQueueAckBatchMaxEvents] = {};
  const uint8_t count = ramQueueAckBatchCount;
  for (uint8_t index = 0U; index < count; ++index) pending[index] = ramQueueAckBatch[index];
  ramQueueAckBatchCount = 0U;
  return postQueueAckBatch(cfg, pending, count);
}

static bool sendQueueEvent(
  Config &cfg,
  inktime::QueueEvent event,
  bool displaySkipped = false,
  const String &errorCode = String(""),
  bool delayedTerminal = false
) {
  if (terminalQueueAck(event)) {
    INK_LOG_INFO("queue_ack_terminal", inktime::queueEventName(event));
  } else {
    INK_LOG_DEBUG("queue_ack_queued", inktime::queueEventName(event));
  }
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
  if (!pending.valid) return false;
  if (runtimeTelemetry.ack_event_count < UINT32_MAX) ++runtimeTelemetry.ack_event_count;
  if (terminalQueueAck(event) && !persistPendingQueueAck(pending)) return false;
  if (deviceAuthInvalid) return false;
  if (WiFi.status() != WL_CONNECTED) return false;
  if (ramQueueAckBatchCount >= kQueueAckBatchMaxEvents && !flushRamQueueAckBatch(cfg)) {
    if (!terminalQueueAck(event) && !persistPendingQueueAck(pending)) {
      lastDeviceWarningCode = "DEVICE-QUEUE-ACK-DURABILITY";
      lastDeviceWarningMessage = "RAM ACK batch flush failure 且 non-terminal ACK 未能 durable 保留";
    }
    return false;
  }
  ramQueueAckBatch[ramQueueAckBatchCount++] = pending;
  if (terminalQueueAck(event)) return flushRamQueueAckBatch(cfg);
  return ramQueueAckBatchCount < kQueueAckBatchMaxEvents
    || flushRamQueueAckBatch(cfg);
}

static bool resumePendingQueueAck(Config &cfg) {
  PendingQueueAck pending[inktime::kMaxAckJournalEntries] = {};
  const uint8_t count = loadAckJournalEntries(
    pending, inktime::kMaxAckJournalEntries);
  for (uint8_t offset = 0U; offset < count; offset += kQueueAckBatchMaxEvents) {
    const uint8_t remaining = count - offset;
    const uint8_t batchCount = min(remaining, kQueueAckBatchMaxEvents);
    if (!postQueueAckBatch(cfg, pending + offset, batchCount, true)) return false;
  }
  return true;
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

  if (cfg.backend_hostport.length() == 0 || !hasDeviceCredential(cfg)) {
#if DEBUG_LOG
    DBG_PRINTLN("[HTTP] 伺服器或裝置 Token 尚未設定，跳過下載");
#endif
    lastDeviceErrorCode = "DEVICE-CONFIG";
    lastDeviceErrorMessage = "伺服器或裝置 credential 尚未設定";
    return false;
  }

  String base;
  if (!normalizedBackendBase(cfg, base)) return false;
  if (!ensureWakeHttpSession(cfg, base)) return false;
  String manifestUrl = base + String(DEVICE_MANIFEST_PATH);

#if DEBUG_LOG
  DBG_PRINTLN("[HTTP] 取得版本 Manifest（Authorization 已遮蔽）");
#endif

  inktime::DeviceHttpTransport &transport = wakeHttpTransport;
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
  if (!addDeviceAuthorization(manifestHttp, cfg)) {
    manifestHttp.end();
    return false;
  }
  int manifestCode = countedHttpGet(manifestHttp);
  if (handleDeviceAuthStatus(cfg, manifestCode)) {
    manifestHttp.end();
    return false;
  }
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
    if (cfg.delivery_mode == "inktime_offline_schedule"
        && (remoteConfig["schema_version"] | 0) < 3) {
      // A legacy/schema-2 latest manifest is the authoritative convergence
      // response after the server has moved this device out of enhanced
      // offline mode.  Do not leave the stale offline mode in NVS.
      candidate.delivery_mode = "legacy_online";
    }
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
    inktime::DeviceHttpTransport &fileTransport = wakeHttpTransport;
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
    if (!addDeviceAuthorization(fileHttp, cfg)) {
      fileHttp.end();
      continue;
    }
    int code = countedHttpGet(fileHttp);
    if (handleDeviceAuthStatus(cfg, code)) {
      fileHttp.end();
      heap_caps_free(packed);
      return false;
    }
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
      if (received > 0) {
        total += received;
        runtimeTelemetry.download_bytes += static_cast<uint32_t>(received);
      }
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
      if (lastDeviceErrorCode == "FRAME_INVALID_PALETTE_INDEX") {
        heap_caps_free(packed);
        return false;
      }
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
  Config &cfg,
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
  // A verified formal frame is content-addressed by SHA and rotation.  Reuse
  // it for a later schedule instead of downloading and converting the same
  // release again; the caller still emits the normal queue download/hash ACKs.
  uint8_t* cachedFormalFrame = nullptr;
  if (photoPainter.loadFormalFrame(expectedSha.c_str(), rotation, &cachedFormalFrame)) {
    if (cachedFormalFrame != nullptr) heap_caps_free(cachedFormalFrame);
    return true;
  }
  String sessionBase;
  if (!ensureWakeHttpSession(cfg, sessionBase) || sessionBase != base) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-SESSION";
    lastDeviceErrorMessage = "離線排程 Slot 未繫結目前 Wake Backend Origin";
    return false;
  }
  uint8_t* packed = photoPainter.allocateWireBuffer(packedSize);
  if (packed == nullptr) {
    lastDeviceErrorCode = "DEVICE-MEMORY";
    lastDeviceErrorMessage = "無法配置離線排程 Slot 緩衝區";
    return false;
  }
  inktime::DeviceHttpTransport &fileTransport = wakeHttpTransport;
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
  if (!addDeviceAuthorization(fileHttp, cfg)) {
    fileHttp.end();
    heap_caps_free(packed);
    return false;
  }
  const int fileStatus = countedHttpGet(fileHttp);
  if (handleDeviceAuthStatus(cfg, fileStatus)) {
    fileHttp.end();
    heap_caps_free(packed);
    return false;
  }
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
    if (received > 0) {
      total += static_cast<size_t>(received);
      runtimeTelemetry.download_bytes += static_cast<uint32_t>(received);
    }
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
  const bool converted = photoPainter.convertFrame(
    packed,
    packedSize,
    indexed4,
    rotation,
    &nativeFrame);
  heap_caps_free(packed);
  if (!converted || nativeFrame == nullptr) {
    lastDeviceErrorCode = photoPainter.lastError();
    if (lastDeviceErrorCode.length() == 0U) lastDeviceErrorCode = "DEVICE-OFFLINE-FRAME";
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
  recordNvsWrite();
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
  recordNvsWrite();
  if (!photoPainter.writeActiveSchedule(scheduleJson.c_str(), scheduleJson.length())
      || photoPainter.activeScheduleId() != scheduleId) {
    return failOfflineScheduleTransaction("離線排程 active schedule 寫入或身分驗證失敗");
  }
  journal.phase = inktime::configstore::JournalPhase::SchedulePromoted;
  if (!configStore.writeJournal(journal, persistError)) {
    errorOut = persistError;
    return failOfflineScheduleTransaction("離線排程 promotion journal 無法寫入");
  }
  recordNvsWrite();
  if (!configStore.commit(prepared, persistError)) {
    errorOut = persistError;
    return failOfflineScheduleTransaction("離線排程 Config A/B pointer 無法 commit");
  }
  recordNvsWrite();
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
  if (cfg.backend_hostport.length() == 0 || !hasDeviceCredential(cfg)
      || !normalizedBackendBase(cfg, base)) {
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-CONFIG";
    lastDeviceErrorMessage = "離線排程缺少 Backend 或裝置 credential";
    return false;
  }
  if (!ensureWakeHttpSession(cfg, base)) return false;
  inktime::DeviceHttpTransport &scheduleTransport = wakeHttpTransport;
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
  if (!addDeviceAuthorization(scheduleHttp, cfg)) {
    scheduleHttp.end();
    return false;
  }
  const int status = countedHttpGet(scheduleHttp);
  if (handleDeviceAuthStatus(cfg, status)) {
    scheduleHttp.end();
    return false;
  }
  const int length = scheduleHttp.getSize();
  const String contentType = scheduleHttp.header("Content-Type");
  if (status == HTTP_CODE_CONFLICT && length > 0 && length <= 4096
      && contentType.startsWith("application/json")) {
    JsonDocument mismatch;
    const DeserializationError mismatchError = deserializeJson(mismatch, scheduleHttp.getStream());
    const String mismatchName = mismatch["error"] | "";
    const String mismatchCode = mismatch["error_code"] | "";
    scheduleHttp.end();
    if (!mismatchError && !mismatch.overflowed()
        && mismatchName == "delivery_mode_mismatch"
        && mismatchCode == "DEVICE-008") {
      offlineDeliveryModeMismatchDetected = true;
      lastDeviceErrorCode = "DEVICE-DELIVERY-MODE-MISMATCH";
      lastDeviceErrorMessage = "伺服器已切換 delivery_mode；本輪只執行一次 latest config convergence";
      return false;
    }
    lastDeviceErrorCode = "DEVICE-OFFLINE-SCHEDULE-HTTP";
    lastDeviceErrorMessage = "離線排程 409 body 不是受信任的 delivery_mode_mismatch contract";
    return false;
  }
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
  const String syncStrategy = schedule["sync_strategy"] | "first_display_lead";
  const String syncTime = schedule["sync_time"] | "";
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
      || !validSyncStrategy(syncStrategy, syncTime)
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
  scheduleCandidate.sync_strategy = syncStrategy;
  scheduleCandidate.sync_time = syncTime;
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
  const bool targetScheduleActive = activeScheduleId == journal.target_schedule_id.c_str();

  if (journal.phase == inktime::configstore::JournalPhase::Prepared
      && !targetScheduleActive
      && (previousPointer || (!activePresent && journal.previous_active_slot == 0))) {
    String clearError;
    if (!configStore.clearJournal(clearError)) {
      return failOfflineScheduleTransaction("離線排程 stale prepared journal 無法清除");
    }
    recordNvsWrite();
    return true;
  }
  if (journal.phase != inktime::configstore::JournalPhase::SchedulePromoted) {
    return failOfflineScheduleTransaction("離線排程 transaction 未完成 promotion，已 fail-closed");
  }
  if (activeScheduleId.length() == 0U || !targetScheduleActive) {
    return failOfflineScheduleTransaction("離線排程 active 身分與 recovery target 不一致");
  }
  if (!activePresent) {
    return failOfflineScheduleTransaction("離線排程 active schedule 存在但 Config pointer 遺失");
  }
  String commitError;
  if (!configStore.commitPreparedSlot(
        journal.prepared_slot,
        journal.prepared_generation,
        preparedPayload,
        commitError)) {
    return failOfflineScheduleTransaction("離線排程 recovery 無法 commit prepared Config");
  }
  recordNvsWrite();
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
  if (!ensureWakeHttpSession(cfg, base)) return QueueDownloadResult::Failed;

  inktime::DeviceHttpTransport &manifestTransport = wakeHttpTransport;
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
  if (!addDeviceAuthorization(manifestHttp, cfg)) {
    manifestHttp.end();
    return QueueDownloadResult::Failed;
  }
  const int status = countedHttpGet(manifestHttp);
  if (handleDeviceAuthStatus(cfg, status)) {
    manifestHttp.end();
    return QueueDownloadResult::Failed;
  }
  if (status == HTTP_CODE_NOT_FOUND || status == HTTP_CODE_CONFLICT) {
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

  inktime::DeviceHttpTransport &fileTransport = wakeHttpTransport;
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
  if (!addDeviceAuthorization(fileHttp, cfg)) {
    fileHttp.end();
    heap_caps_free(packed);
    return QueueDownloadResult::Failed;
  }
  const int fileStatus = countedHttpGet(fileHttp);
  if (handleDeviceAuthStatus(cfg, fileStatus)) {
    fileHttp.end();
    heap_caps_free(packed);
    return QueueDownloadResult::Failed;
  }
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
    if (received > 0) {
      total += static_cast<size_t>(received);
      runtimeTelemetry.download_bytes += static_cast<uint32_t>(received);
    }
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
  const String syncStrategy = active["sync_strategy"] | "first_display_lead";
  const String syncTime = active["sync_time"] | "";
  if (!rawEnd.is<int64_t>() || rawEnd.is<bool>() || rawEnd.as<int64_t>() <= 0
      || !rawSlots.is<JsonArrayConst>()
      || !validSyncStrategy(syncStrategy, syncTime)) return false;
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

static bool validatedActiveScheduleVersion(
    const Config& cfg, time_t nowEpoch, uint32_t& versionOut) {
  versionOut = 0U;
  String activeJson;
  if (!photoPainter.readActiveSchedule(activeJson)) return false;
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
  const String activeSyncStrategy = active["sync_strategy"] | "first_display_lead";
  const String activeSyncTime = active["sync_time"] | "";
  const JsonVariantConst activeSchema = active["schema_version"];
  const JsonVariantConst activeConfigVersion = active["config_version"];
  const JsonVariantConst activeRotation = active["rotation"];
  const JsonVariantConst activeScheduleVersion = active["offline_schedule_version"];
  const JsonVariantConst activeTargetStartEpoch = active["target_start_epoch"];
  const JsonVariantConst activeTargetEndEpoch = active["target_end_epoch"];
  if (jsonError || active.overflowed() || !rawSlots.is<JsonArrayConst>()
      || targetDate.length() != 10U || activeTargetDate.length() == 0U
      || (activeLegacyDate.length() > 0U && activeLegacyDate != activeTargetDate)
      || !activeSchema.is<int32_t>() || activeSchema.is<bool>()
      || activeSchema.as<int32_t>() != inktime::kOfflineScheduleSchemaVersion
      || activeDeliveryMode != "inktime_offline_schedule"
      || activeTimezone.length() == 0U || activeTimezone.length() > 64U
      || activeScheduleId.length() == 0U || !inktime::boundedText(
        activeScheduleId.c_str(), inktime::kQueueIdentifierMaxBytes)
      || !validSyncStrategy(activeSyncStrategy, activeSyncTime)
      || !activeConfigVersion.is<uint32_t>() || activeConfigVersion.is<bool>()
      || !activeRotation.is<int32_t>() || activeRotation.is<bool>()
      || !activeScheduleVersion.is<int32_t>() || activeScheduleVersion.is<bool>()
      || activeScheduleVersion.as<int32_t>() < 0
      || !activeTargetStartEpoch.is<int64_t>() || activeTargetStartEpoch.is<bool>()
      || !activeTargetEndEpoch.is<int64_t>() || activeTargetEndEpoch.is<bool>()
      || activeTargetStartEpoch.as<int64_t>() <= 0
      || activeTargetEndEpoch.as<int64_t>() <= activeTargetStartEpoch.as<int64_t>()
      || nowEpoch <= 0) {
    return false;
  }
  const int64_t targetStartEpoch = activeTargetStartEpoch.as<int64_t>();
  const int64_t targetEndEpoch = activeTargetEndEpoch.as<int64_t>();
  if (static_cast<int64_t>(nowEpoch) < targetStartEpoch
      || static_cast<int64_t>(nowEpoch) >= targetEndEpoch) return false;

  const JsonArrayConst slots = rawSlots.as<JsonArrayConst>();
  JsonArrayConst rawTimes = active["schedule_times"].as<JsonArrayConst>();
  if (rawTimes.isNull()) rawTimes = active["schedule"].as<JsonArrayConst>();
  if (rawTimes.isNull() || rawTimes.size() == 0U
      || rawTimes.size() > inktime::kMaxOfflineSlots || rawTimes.size() != slots.size()) {
    return false;
  }
  inktime::OfflineSlot activeScheduleSlots[inktime::kMaxOfflineSlots] = {};
  for (size_t index = 0; index < rawTimes.size(); ++index) {
    if (!parseOfflineClock(rawTimes[index] | "", activeScheduleSlots[index])) return false;
  }
  if (!inktime::validateOfflineSlots(
        activeScheduleSlots, static_cast<uint8_t>(rawTimes.size()))
      || rawTimes.size() != cfg.schedule_count
      || !inktime::validateOfflineSlots(cfg.schedule_slots, cfg.schedule_count)) {
    return false;
  }
  for (uint8_t index = 0; index < cfg.schedule_count; ++index) {
    if (activeScheduleSlots[index].hour != cfg.schedule_slots[index].hour
        || activeScheduleSlots[index].minute != cfg.schedule_slots[index].minute) {
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
    return false;
  }

  int64_t previousShowAtEpoch = 0;
  for (size_t index = 0U; index < slots.size(); ++index) {
    const JsonVariantConst rawSlot = slots[index];
    if (!rawSlot.is<JsonObjectConst>()) return false;
    const JsonObjectConst slot = rawSlot.as<JsonObjectConst>();
    const JsonVariantConst rawIndex = slot["slot_index"];
    const JsonVariantConst rawQueueVersion = slot["queue_version"];
    const JsonVariantConst rawShowAt = slot["show_at_epoch"];
    const String slotScheduleId = slot["offline_schedule_id"] | "";
    const String queueItemId = slot["queue_item_id"] | "";
    const String releaseId = slot["release_id"] | "";
    const String sha = slot["sha256"] | "";
    const String renderProfile = slot["render_profile"] | "";
    if (!rawIndex.is<int32_t>() || rawIndex.is<bool>()
        || rawIndex.as<int32_t>() != static_cast<int32_t>(index)
        || !rawQueueVersion.is<int32_t>() || rawQueueVersion.is<bool>()
        || rawQueueVersion.as<int32_t>() < 0
        || !rawShowAt.is<int64_t>() || rawShowAt.is<bool>()
        || rawShowAt.as<int64_t>() < targetStartEpoch
        || rawShowAt.as<int64_t>() >= targetEndEpoch
        || (index > 0U && rawShowAt.as<int64_t>() <= previousShowAtEpoch)
        || slotScheduleId != activeScheduleId
        || !inktime::boundedText(queueItemId.c_str(), inktime::kQueueIdentifierMaxBytes)
        || !inktime::boundedText(releaseId.c_str(), inktime::kQueueIdentifierMaxBytes)
        || !inktime::isSha256Hex(sha.c_str())
        || (renderProfile != "safe_4c" && renderProfile != String(INKTIME_PANEL_PROFILE))) {
      return false;
    }
    previousShowAtEpoch = rawShowAt.as<int64_t>();
  }
  versionOut = static_cast<uint32_t>(activeScheduleVersion.as<int32_t>());
  return true;
}

static time_t fixedDailySyncEpoch(const String& syncTime, time_t nowEpoch) {
  if (!validSyncTime(syncTime) || syncTime.isEmpty() || nowEpoch <= 0) return 0;
  struct tm localNow = {};
  localtime_r(&nowEpoch, &localNow);
  struct tm candidate = localNow;
  candidate.tm_hour = syncTime.substring(0, 2).toInt();
  candidate.tm_min = syncTime.substring(3, 5).toInt();
  candidate.tm_sec = 0;
  time_t candidateEpoch = mktime(&candidate);
  if (candidateEpoch <= nowEpoch) {
    candidate.tm_mday += 1;
    candidateEpoch = mktime(&candidate);
  }
  return candidateEpoch > nowEpoch ? candidateEpoch : 0;
}

static bool fixedDailySyncDue(const String& syncTime, time_t nowEpoch) {
  if (!validSyncTime(syncTime) || syncTime.isEmpty() || nowEpoch <= 0) return false;
  struct tm localNow = {};
  localtime_r(&nowEpoch, &localNow);
  struct tm candidate = localNow;
  candidate.tm_hour = syncTime.substring(0, 2).toInt();
  candidate.tm_min = syncTime.substring(3, 5).toInt();
  candidate.tm_sec = 0;
  const time_t candidateEpoch = mktime(&candidate);
  constexpr time_t kFixedDailyDueWindowSeconds = 5 * 60;
  return candidateEpoch > 0 && nowEpoch >= candidateEpoch
    && nowEpoch - candidateEpoch <= kFixedDailyDueWindowSeconds;
}

static bool runtimeSyncFields(
    const Config& cfg, String& strategyOut, String& syncTimeOut) {
  strategyOut = cfg.sync_strategy;
  syncTimeOut = cfg.sync_time;
  String activeJson;
  JsonDocument active;
  if (photoPainter.readActiveSchedule(activeJson)
      && !deserializeJson(active, activeJson) && !active.overflowed()) {
    strategyOut = active["sync_strategy"] | strategyOut;
    syncTimeOut = active["sync_time"] | syncTimeOut;
  }
  if (strategyOut.isEmpty()) strategyOut = "first_display_lead";
  return validSyncStrategy(strategyOut, syncTimeOut);
}

static bool offlineFixedDailySyncDue(const Config& cfg, time_t nowEpoch) {
  if (cfg.delivery_mode != "inktime_offline_schedule") return false;
  String strategy;
  String syncTime;
  if (!runtimeSyncFields(cfg, strategy, syncTime)) return false;
  return strategy == "fixed_daily" && fixedDailySyncDue(syncTime, nowEpoch);
}

#endif

static void nextRuntimeEpochs(
    const Config& cfg,
    time_t nowEpoch,
    int64_t& nextWakeOut,
    int64_t& nextSyncOut) {
  nextWakeOut = 0;
  nextSyncOut = 0;
  if (nowEpoch <= 0) return;
#if INKTIME_PHOTOPAINTER_ENABLED
  if (cfg.delivery_mode == "inktime_offline_schedule") {
    String strategy;
    String syncTime;
    if (!runtimeSyncFields(cfg, strategy, syncTime)) return;
    if (strategy == "fixed_daily") {
      nextSyncOut = fixedDailySyncEpoch(syncTime, nowEpoch);
    } else {
      time_t nextDisplay = 0;
      time_t targetEnd = 0;
      time_t nextSchedulePrefetch = 0;
      if (activeOfflineEpochs(nowEpoch, nextDisplay, targetEnd, nextSchedulePrefetch)) {
        if (nextDisplay > nowEpoch) {
          const time_t lead = static_cast<time_t>(cfg.prefetch_lead_minutes) * 60;
          const time_t displaySync = nextDisplay > lead ? nextDisplay - lead : nextDisplay;
          if (displaySync > nowEpoch) nextSyncOut = displaySync;
        }
        if (nextSchedulePrefetch > nowEpoch
            && (nextSyncOut == 0 || nextSchedulePrefetch < nextSyncOut)) {
          nextSyncOut = nextSchedulePrefetch;
        }
      }
    }
    time_t nextDisplay = 0;
    time_t targetEnd = 0;
    time_t nextSchedulePrefetch = 0;
    if (activeOfflineEpochs(nowEpoch, nextDisplay, targetEnd, nextSchedulePrefetch)) {
      const auto consider = [nowEpoch, &nextWakeOut](time_t candidate) {
        if (candidate > nowEpoch && (nextWakeOut == 0 || candidate < nextWakeOut)) {
          nextWakeOut = candidate;
        }
      };
      if (nextDisplay > nowEpoch) {
        const time_t lead = static_cast<time_t>(cfg.prefetch_lead_minutes) * 60;
        consider(nextDisplay > lead ? nextDisplay - lead : nextDisplay);
      }
      consider(nextSchedulePrefetch);
      consider(targetEnd);
      if (nextSyncOut > 0) consider(static_cast<time_t>(nextSyncOut));
    }
    if (nextSyncOut > nowEpoch
        && (nextWakeOut == 0 || nextSyncOut < nextWakeOut)) {
      nextWakeOut = nextSyncOut;
    }
    return;
  }
#endif
  struct tm localNow = {};
  localtime_r(&nowEpoch, &localNow);
  struct tm candidate = localNow;
  candidate.tm_hour = cfg.refresh_hour;
  candidate.tm_min = cfg.refresh_minute;
  candidate.tm_sec = 0;
  time_t candidateEpoch = mktime(&candidate);
  if (candidateEpoch <= nowEpoch) {
    candidate.tm_mday += 1;
    candidateEpoch = mktime(&candidate);
  }
  if (candidateEpoch > nowEpoch) nextWakeOut = candidateEpoch;
}

static String wakeReasonDetail() {
  switch (esp_sleep_get_wakeup_cause()) {
    case ESP_SLEEP_WAKEUP_TIMER: return "timer";
    case ESP_SLEEP_WAKEUP_EXT0:
    case ESP_SLEEP_WAKEUP_EXT1: return "user_button_or_external";
    case ESP_SLEEP_WAKEUP_UNDEFINED: return "power_on";
    default: return "other";
  }
}

#if INKTIME_PHOTOPAINTER_ENABLED
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
  String syncStrategy;
  String syncTime;
  if (!runtimeSyncFields(cfg, syncStrategy, syncTime)) return true;
  time_t nextDisplay = 0;
  time_t targetEnd = 0;
  time_t nextSchedulePrefetch = 0;
  if (!activeOfflineEpochs(nowEpoch, nextDisplay, targetEnd, nextSchedulePrefetch)) return true;
  if (targetEnd > 0 && nowEpoch >= targetEnd) return true;
  if (nextSchedulePrefetch > 0 && nowEpoch >= nextSchedulePrefetch
      && (targetEnd <= 0 || nowEpoch < targetEnd)) return true;
  if (syncStrategy == "fixed_daily") return false;
  if (cfg.prefetch_lead_minutes == 0) {
    // A zero lead is a valid first_display_lead contract: the advertised
    // network-sync epoch is the display boundary, so that boundary must take
    // the network path instead of silently becoming a local-only wake.
    return activeHasDueFormalSlot(nowEpoch)
      && (targetEnd <= 0 || nowEpoch < targetEnd);
  }
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
  const String syncStrategy = staged["sync_strategy"] | "first_display_lead";
  const String syncTime = staged["sync_time"] | "";
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
      || !validButtonWakeAction(buttonWakeAction)
      || !validSyncStrategy(syncStrategy, syncTime)
      || remoteLead < 0 || remoteLead > 120
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
  candidate.sync_strategy = syncStrategy;
  candidate.sync_time = syncTime;
  candidate.config_version = remoteConfigVersion;
  String persistError;
  inktime::DeviceConfigStore::Prepared prepared;
  if (!configStore.prepare(configPayload(candidate), prepared, persistError)) {
    setConfigPersistenceError(persistError);
    return false;
  }
  recordNvsWrite();
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
  recordNvsWrite();
  if (!photoPainter.promoteStagedNextSchedule()
      || photoPainter.activeScheduleId() != scheduleId) {
    return failOfflineScheduleTransaction("離線排程 staged next promote 或身分驗證失敗");
  }
  journal.phase = inktime::configstore::JournalPhase::SchedulePromoted;
  if (!configStore.writeJournal(journal, persistError)
      || !configStore.commit(prepared, persistError)) {
    return failOfflineScheduleTransaction("離線排程 midnight Config commit 失敗");
  }
  recordNvsWrite();
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
  offlineDeliveryModeMismatchDetected = false;
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
      if (offlineDeliveryModeMismatchDetected) {
        currentPrefetchOnly = false;
        const bool latest = downloadLatestPhotoBin(cfg);
        if (latest) currentDisplaySkipped = shouldSkipCurrentDisplay(cfg);
        return latest;
      }
      currentPrefetchOnly = false;
      return loadOfflineScheduledLocalFrame(cfg, time(nullptr), false);
    }
    if (timerRequestedNetwork || offlinePrefetchWake(cfg, time(nullptr))) {
      const time_t wakeEpoch = time(nullptr);
      const bool formalSlotDue = activeHasDueFormalSlot(wakeEpoch);
      const bool stageTomorrow = offlineNextSchedulePrefetchDue(cfg, wakeEpoch)
        && !formalSlotDue;
      const bool displayAtThisWake = formalSlotDue;
      currentPrefetchOnly = stageTomorrow || !displayAtThisWake;
      const bool prefetched = downloadOfflineScheduleAndFrames(cfg, stageTomorrow);
      if (offlineDeliveryModeMismatchDetected) {
        currentPrefetchOnly = false;
        const bool latest = downloadLatestPhotoBin(cfg);
        if (latest) currentDisplaySkipped = shouldSkipCurrentDisplay(cfg);
        return latest;
      }
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
    if (currentFromQueue) {
      sendQueueEvent(cfg, inktime::QueueEvent::DisplayFailed, false, lastDeviceErrorCode);
    }
    return false;
  }
  const bool latest = downloadLatestPhotoBin(cfg);
  if (latest) currentDisplaySkipped = shouldSkipCurrentDisplay(cfg);
  return latest;
}

static void appendStatusReportedAt(JsonDocument &payload, time_t epoch) {
  // time(nullptr) is the device RTC/NTP authority used by the offline
  // schedule contract.  Do not emit an uninitialized epoch: the server must
  // keep its normal receive-time fallback until the clock is trustworthy.
  if (epoch < static_cast<time_t>(1600000000)) return;
  struct tm utcTime = {};
  if (gmtime_r(&epoch, &utcTime) == nullptr) return;
  char reportedAt[25] = {};
  if (strftime(reportedAt, sizeof(reportedAt), "%Y-%m-%dT%H:%M:%SZ", &utcTime) == 0) return;
  payload["status_reported_at"] = reportedAt;
}

void reportDeviceStatus(Config &cfg, bool displayUpdated) {
  if (WiFi.status() != WL_CONNECTED || cfg.backend_hostport.length() == 0 || !hasDeviceCredential(cfg)) return;
  String base;
  if (!normalizedBackendBase(cfg, base)) return;
  if (!ensureWakeHttpSession(cfg, base)) return;

  if (networkSessionStartedMs == 0U) networkSessionStartedMs = millis();
  runtimeTelemetry.network_session_ms = millis() - networkSessionStartedMs;
  const time_t telemetryNow = time(nullptr);
  nextRuntimeEpochs(
    cfg, telemetryNow, runtimeTelemetry.next_wake_epoch, runtimeTelemetry.next_network_sync_epoch);

#if INKTIME_PHOTOPAINTER_ENABLED
  photoPainter.readEnvironment();
  uint32_t validatedScheduleVersion = 0U;
  if (validatedActiveScheduleVersion(cfg, telemetryNow, validatedScheduleVersion)) {
    runtimeTelemetry.applied_offline_schedule_version = validatedScheduleVersion;
  }
  runtimeTelemetry.epd_transfer_ms = photoPainter.lastRefreshDurationMs();
  runtimeTelemetry.i2c_retry_count = photoPainter.i2cRetryCount();
  runtimeTelemetry.i2c_bus_reset_count = photoPainter.i2cBusResetCount();
  runtimeTelemetry.i2c_fail_closed_count = photoPainter.i2cFailClosedCount();
  runtimeTelemetry.gc_deleted_files = photoPainter.gcDeletedFiles();
  runtimeTelemetry.gc_deleted_bytes = photoPainter.gcDeletedBytes();
  runtimeTelemetry.gc_skipped_protected = photoPainter.gcSkippedProtected();
#endif
  JsonDocument payload;
  payload["firmware_version"] = INKTIME_FIRMWARE_VERSION;
  payload["board_profile"] = kBoardConfig.name;
  payload["wifi_rssi"] = WiFi.RSSI();
  payload["free_heap_bytes"] = ESP.getFreeHeap();
  payload["free_psram_bytes"] = ESP.getFreePsram();
  payload["wake_reason"] = String((int)esp_sleep_get_wakeup_cause());
  payload["wake_reason_detail"] = wakeReasonDetail();
  appendStatusReportedAt(payload, telemetryNow);
  payload["wifi_connect_ms"] = runtimeTelemetry.wifi_connect_ms;
  payload["wifi_fast_path_attempted"] = runtimeTelemetry.wifi_fast_path_attempted;
  payload["wifi_fast_path_success"] = runtimeTelemetry.wifi_fast_path_success;
  payload["network_session_ms"] = runtimeTelemetry.network_session_ms;
  payload["http_request_count"] = runtimeTelemetry.http_request_count;
  payload["ntp_sync_attempted"] = runtimeTelemetry.ntp_sync_attempted;
  payload["ntp_sync_succeeded"] = runtimeTelemetry.ntp_sync_succeeded;
  payload["ntp_sync_ms"] = runtimeTelemetry.ntp_sync_ms;
  payload["download_bytes"] = runtimeTelemetry.download_bytes;
  payload["nvs_write_count"] = runtimeTelemetry.nvs_write_count;
  payload["ack_event_count"] = runtimeTelemetry.ack_event_count;
  payload["ack_batch_request_count"] = runtimeTelemetry.ack_batch_request_count;
  payload["i2c_retry_count"] = runtimeTelemetry.i2c_retry_count;
  payload["i2c_bus_reset_count"] = runtimeTelemetry.i2c_bus_reset_count;
  payload["i2c_fail_closed_count"] = runtimeTelemetry.i2c_fail_closed_count;
  payload["gc_deleted_files"] = runtimeTelemetry.gc_deleted_files;
  payload["gc_deleted_bytes"] = runtimeTelemetry.gc_deleted_bytes;
  payload["gc_skipped_protected"] = runtimeTelemetry.gc_skipped_protected;
  payload["tls_handshake_count_unavailable"] = runtimeTelemetry.tls_handshake_count_unavailable;
  payload["tls_handshake_count_unavailable_reason"] =
      runtimeTelemetry.tls_handshake_count_unavailable_reason;
  if (runtimeTelemetry.next_wake_epoch > 0) {
    payload["next_wake_epoch"] = runtimeTelemetry.next_wake_epoch;
  } else {
    payload["next_wake_epoch"] = nullptr;
  }
  if (runtimeTelemetry.next_network_sync_epoch > 0) {
    payload["next_network_sync_epoch"] = runtimeTelemetry.next_network_sync_epoch;
  } else {
    payload["next_network_sync_epoch"] = nullptr;
  }
  if (runtimeTelemetry.applied_offline_schedule_version > 0U) {
    payload["applied_offline_schedule_version"] = runtimeTelemetry.applied_offline_schedule_version;
  } else {
    payload["applied_offline_schedule_version"] = nullptr;
  }
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
  payload["warning_code"] = lastDeviceWarningCode;
  payload["warning_message"] = lastDeviceWarningMessage;
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
  const inktime::PowerSourceState powerSourceState = photoPainter.powerSourceState();
  if (powerSourceState != inktime::PowerSourceState::Unknown) {
    payload["usb_power"] = powerSourceState == inktime::PowerSourceState::Usb;
  }
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
  payload["sd_read_bytes"] = photoPainter.sdReadBytes();
  payload["sd_write_bytes"] = photoPainter.sdWriteBytes();
  payload["sd_write_ms"] = photoPainter.sdWriteDurationMs();
#endif
  payload["last_refresh_duration_ms"] = lastRefreshDurationMs;
  payload["epd_transfer_ms"] = runtimeTelemetry.epd_transfer_ms;
  const uint32_t awakeTotalMs = millis();
  payload["awake_total_ms"] = awakeTotalMs;
  payload["wake_duration_ms"] = awakeTotalMs;
  String body;
  serializeJson(payload, body);

  inktime::DeviceHttpTransport &statusTransport = wakeHttpTransport;
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
  if (!addDeviceAuthorization(statusHttp, cfg)) {
    statusHttp.end();
    return;
  }
  statusHttp.addHeader("Content-Type", "application/json");
  const int status = countedHttpPost(statusHttp, body);
  (void)handleDeviceAuthStatus(cfg, status);
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

#if !INKTIME_PHOTOPAINTER_ENABLED
static bool displayPairingCode(const Config &cfg, const String &pairingCode) {
  initDisplay(cfg);
  const uint32_t refreshStarted = millis();
  display.setFullWindow();
  display.firstPage();
  do {
    display.fillScreen(GxEPD_WHITE);
    display.setTextColor(GxEPD_BLACK);
    display.setTextSize(2);
    display.setCursor(20, 90);
    display.print("INKTIME PAIRING");
    display.setCursor(20, 180);
    display.print("CODE:");
    display.setCursor(20, 235);
    display.setTextSize(4);
    display.print(pairingCode);
    display.setTextSize(2);
    display.setCursor(20, 330);
    display.print("VALID 5 MIN");
  } while (display.nextPage());
  display.hibernate();
  lastRefreshDurationMs = millis() - refreshStarted;
  return true;
}
#endif

bool drawFromFrameData(const Config &cfg) {
  (void)cfg;

#if INKTIME_PHOTOPAINTER_ENABLED
  if (!frameNativePalette || frameDataSize != inktime::kPhotoPainterFrameBytes) return false;
  const bool updated = photoPainter.displayFrame(frameData, frameDataSize);
  lastRefreshDurationMs = photoPainter.lastRefreshDurationMs();
  runtimeTelemetry.epd_transfer_ms = lastRefreshDurationMs;
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
  runtimeTelemetry.epd_transfer_ms = lastRefreshDurationMs;
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
        String syncStrategy;
        String syncTime;
        if (runtimeSyncFields(cfg, syncStrategy, syncTime)
            && syncStrategy == "fixed_daily") {
          considerWake(fixedDailySyncEpoch(syncTime, nowEpoch));
        }
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
  const String activeSyncStrategy = active["sync_strategy"] | "first_display_lead";
  const String activeSyncTime = active["sync_time"] | "";
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
      || !validSyncStrategy(activeSyncStrategy, activeSyncTime)
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
      stopNetworkBeforeDisplay();
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
  runtimeTelemetry = RuntimeTelemetry{};
  networkSessionStartedMs = 0;
  networkClosedForDisplay = false;

  setCpuFrequencyMhz(80);
  if (kBoardConfig.statusLed != inktime::kNoPin) {
    pinMode(kBoardConfig.statusLed, OUTPUT);
    digitalWrite(kBoardConfig.statusLed, LOW);
  }

  DBG_BEGIN();
  delay(200);
  INK_LOG_INFO("firmware_boot", "InkTime firmware boot started");

#if INKTIME_PHOTOPAINTER_ENABLED
  startMaxAwakeSupervisor();
#endif

#if DEBUG_LOG
  DBG_PRINTLN();
  DBG_PRINTLN("===== ESP32-S3 InkTime Daily Photo boot =====");
#if INKTIME_ALLOW_INSECURE_DEVICE_HTTP
  DBG_PRINTLN("[SECURITY] trusted-LAN HTTP build：僅限可信任 LAN／IoT VLAN，沒有 TLS");
#endif
#endif

  const bool factoryResetRequested = isFactoryResetRequestedAtBoot();
  if (factoryResetRequested) {
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
    INK_LOG_ERROR("photopainter_init_failed", photoPainter.lastError());
  } else {
    INK_LOG_INFO("photopainter_ready", "PhotoPainter Flash and PSRAM checks passed");
    if (!photoPainter.sdReady()) {
      INK_LOG_WARN("photopainter_sd_unavailable", "SD cache is unavailable; continuing without SD cache");
    }
    if (!photoPainter.rtcReady()) {
      INK_LOG_WARN("photopainter_rtc_unavailable", "RTC is unavailable; network time remains required");
    }
    if (!photoPainter.shtc3Ready()) {
      INK_LOG_WARN("photopainter_sensor_unavailable", "SHTC3 telemetry is unavailable");
    }
  }
  // Recovery is a deliberate hold distinct from the established shorter
  // force-network-refresh gesture. A short wake never authorizes service.
  const bool explicitRecoveryRequested = factoryResetRequested
      || photoPainter.recoveryServiceRequested();
  const uint32_t maxAwakeRecoveryCount =
      inktime::maxAwakeRecoveryCount(maxAwakeRecoveryState);
  if (inktime::shouldEnterMaxAwakeSafeSleep(maxAwakeRecoveryState)
      && !explicitRecoveryRequested) {
    INK_LOG_WARN(
      "max_awake_safe_sleep",
      "Repeated max-awake recovery reached its limit; entering one-hour safe sleep"
    );
    goDeepSleepSeconds(inktime::kMaxAwakeSafeSleepSeconds);
    return;
  }
  if (explicitRecoveryRequested && maxAwakeRecoveryCount > 0U) {
    // A deliberate GPIO4 recovery hold or factory reset is allowed to break
    // the automatic backoff without an NVS write.
    inktime::resetMaxAwakeRecoveryState(maxAwakeRecoveryState);
  } else if (maxAwakeRecoveryCount > 0U) {
    INK_LOG_WARN(
      "max_awake_recovery_retry",
      "Retrying after a max-awake supervisor restart"
    );
  }
#endif

  loadConfig(g_cfg);

  if (g_cfg.valid) {
    struct tm bootTime = {};
    (void)seedTimeFromRtc(g_cfg, bootTime);
  }

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

#if INKTIME_PHOTOPAINTER_ENABLED
  runFormalFrameGcForWake();
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
  if (!offlineScheduleTxnBlocked && g_cfg.auth_state == "paired" && !deviceAuthInvalid
      && g_cfg.delivery_mode == "inktime_offline_schedule"
      && photoPainter.wokeFromUserButton()
      && !photoPainter.forceNetworkRefresh()
      && g_cfg.button_wake_action == "local_next") {
    // local_next is a strict cache-only action.  It must not connect Wi-Fi or
    // ask the generic queue for its first item.
    runOfflineLocalCycle(true);
    return;
  }
  if (!offlineScheduleTxnBlocked && g_cfg.auth_state == "paired" && !deviceAuthInvalid
      && g_cfg.delivery_mode == "inktime_offline_schedule" && timerWake
      && !photoPainter.forceNetworkRefresh()) {
    time_t rtcEpoch = 0;
    if (!photoPainter.readRtc(rtcEpoch)) {
      runOfflineLocalCycle();
      return;
    }
    applyFixedTimezoneWithoutNtp(g_cfg.tz_offset_minutes);
    struct timeval value = {rtcEpoch, 0};
    settimeofday(&value, nullptr);
    const bool networkWake = offlinePrefetchWake(g_cfg, rtcEpoch)
      || offlineFixedDailySyncDue(g_cfg, rtcEpoch);
    if (!networkWake) {
      runOfflineLocalCycle();
      return;
    }
    enhancedNetworkWakeRequested = true;
  }
#endif

  if ((g_cfg.auth_state == "auth_invalid" || g_cfg.auth_state == "revoked")
      && !pairingRetryDue(g_cfg)) {
    goDeepSleepSeconds(pairingBackoffSeconds(g_cfg));
    return;
  }

#if DEBUG_LOG
  DBG_PRINTLN("[BOOT] have config -> connect WiFi");
#endif
  networkSessionStartedMs = millis();
  if (!connectWiFi(g_cfg)) {
    INK_LOG_WARN("wifi_degraded_mode", "Wi-Fi unavailable; selecting bounded recovery path");
#if DEBUG_LOG
    DBG_PRINTLN("[BOOT] connect failed");
#endif
#if INKTIME_PHOTOPAINTER_ENABLED
    applyFixedTimezoneWithoutNtp(g_cfg.tz_offset_minutes);
    time_t rtcEpoch = 0;
    struct tm offlineTime = {};
    bool hasOfflineTime = photoPainter.readRtc(rtcEpoch);
    if (hasOfflineTime) {
      struct timeval value = {rtcEpoch, 0};
      settimeofday(&value, nullptr);
      localtime_r(&rtcEpoch, &offlineTime);
    }
    if (!explicitRecoveryRequested && !offlineScheduleTxnBlocked
        && g_cfg.delivery_mode == "inktime_offline_schedule" && hasOfflineTime
        && activeHasDueFormalSlot(rtcEpoch)) {
      // A due 00:00/current formal slot is serviceable from the active
      // cache even when the network recovery attempt fails.
      runOfflineLocalCycle();
      return;
    }
#endif
    DBG_PRINTLN("[BOOT] enter bounded AP portal");
    startConfigPortal();
  }

  // Pairing is a one-time authorization flow, never part of an ordinary
  // credentialed wake.  A revoked/invalid credential may re-enter this flow
  // only after a dedicated authenticated permission probe confirms that the
  // backend has enabled repair; the backend still requires a fresh
  // short-lived pairing code and administrator approval.
  bool repairPermission = false;
  if ((g_cfg.auth_state == "auth_invalid" || g_cfg.auth_state == "revoked")
      && pairingRetryDue(g_cfg)) {
    repairPermission = checkRepairPermission(g_cfg);
    if (!repairPermission) {
      goDeepSleepSeconds(pairingBackoffSeconds(g_cfg));
      return;
    }
  }
  if ((automaticPairingAllowed(g_cfg) || repairPermission) && !performAutomaticPairing(g_cfg)) {
    if (lastDeviceErrorCode.length() == 0U) {
      lastDeviceErrorCode = "DEVICE-PAIRING-RECOVERY";
      lastDeviceErrorMessage = "自動配對尚未完成；本輪停止網路工作並等待 bounded recovery wake";
    }
    goDeepSleepSeconds(pairingBackoffSeconds(g_cfg));
    return;
  }
  if (g_cfg.auth_state != "paired" || !hasDeviceCredential(g_cfg)) {
    lastDeviceErrorCode = "DEVICE-AUTH-CONFIG";
    lastDeviceErrorMessage = "配對尚未完成；不進入未授權的下載或狀態回報流程";
    goDeepSleepSeconds(pairingBackoffSeconds(g_cfg));
    return;
  }

#if INKTIME_PHOTOPAINTER_ENABLED
  if (explicitRecoveryRequested) {
    (void)runUsbServiceMode(explicitRecoveryRequested);
    struct tm recoveryTime = {};
    bool hasRecoveryTime = getLocalTime(&recoveryTime, 1000);
    if (!hasRecoveryTime) hasRecoveryTime = syncTime(g_cfg, recoveryTime);
    sleepUntilNextSchedule(g_cfg, hasRecoveryTime, recoveryTime);
    return;
  }
#endif

  struct tm timeinfo;
  String wakeOrigin;
  (void)ensureWakeHttpSession(g_cfg, wakeOrigin);
  bool hasTime = syncTime(g_cfg, timeinfo);

  bool ok = downloadDailyPhotoBin(g_cfg);
  if (ok) INK_LOG_INFO("payload_ready", "Display payload is ready");
  else INK_LOG_ERROR("payload_download_failed", "Display payload pipeline failed");
  if (serverConfigChanged) hasTime = syncTime(g_cfg, timeinfo);
  bool displayUpdated = false;
  if (ok) {
    if (currentPrefetchOnly) {
      displayUpdated = false;
      (void)flushRamQueueAckBatch(g_cfg);
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
    if (mayDisplay && currentFromQueue) {
      // Download/hash events and DISPLAY_STARTED are RAM-first, but all
      // required ACK work must be attempted before radio shutdown.
      mayDisplay = flushRamQueueAckBatch(g_cfg);
    }
    if (!mayDisplay && currentFromQueue && queueAckPermanentReject) {
      // A stable server-side rejection is durable evidence that retrying this
      // ACK cannot repair the response in the current wake. It must not
      // prevent a verified frame from being displayed; the terminal event is
      // retained for the next delayed-terminal recovery.
      mayDisplay = true;
    }
    if (!mayDisplay && currentFromQueue && !currentDisplaySkipped) {
      const String ackFailure = lastDeviceErrorCode.length() > 0U
        ? lastDeviceErrorCode : String("DEVICE-QUEUE-ACK");
      sendQueueEvent(g_cfg, inktime::QueueEvent::DisplayFailed, false, ackFailure);
    }
    if (mayDisplay && !currentDisplaySkipped) {
      INK_LOG_INFO("display_refresh_started", "Display refresh started");
      stopNetworkBeforeDisplay();
      initDisplay(g_cfg);
      displayUpdated = drawFromFrameData(g_cfg);
    }
    if (displayUpdated) {
      INK_LOG_INFO("display_refresh_completed", "Display refresh completed");
      saveDisplayRecord(g_cfg, true);
      if (currentFromQueue) {
        sendQueueEvent(g_cfg, inktime::QueueEvent::DisplayCompleted);
      }
    } else if (!currentDisplaySkipped && mayDisplay) {
      INK_LOG_ERROR("display_refresh_failed", "Display refresh failed or timed out");
#if INKTIME_PHOTOPAINTER_ENABLED
      lastDeviceErrorCode = photoPainter.lastError();
#else
      lastDeviceErrorCode = "DEVICE-DISPLAY";
#endif
      lastDeviceErrorMessage = "電子紙刷新失敗或逾時";
      saveDisplayRecord(g_cfg, false);
      if (currentFromQueue) {
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
  if (deviceAuthInvalid) {
    // A confirmed 401/403 stops this wake completely.  Do not send a
    // follow-up Status or retry Queue/image work with the rejected credential.
    goDeepSleepSeconds(60U);
    return;
  }
  if (!networkClosedForDisplay) reportDeviceStatus(g_cfg, displayUpdated);

  if (!hasTime) {
    struct tm tmp;
    if (!networkClosedForDisplay && syncTime(g_cfg, tmp)) {
      sleepUntilNextSchedule(g_cfg, true, tmp);
    }
    else                      sleepUntilNextSchedule(g_cfg, false, timeinfo);
  } else {
    sleepUntilNextSchedule(g_cfg, true, timeinfo);
  }
}

void loop() {
  delay(1000);
  goDeepSleepSeconds(60U);
}
