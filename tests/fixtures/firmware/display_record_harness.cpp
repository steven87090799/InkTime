#include <algorithm>
#include <cassert>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <vector>
#include "photopainter_core.h"
#include "content_notice_core.h"
#include "queue_client_core.h"

using inktime::kBoardConfig;

class String {
 public:
  String(const char* value = "") : value_(value) {}
  String(const std::string& value) : value_(value) {}
  const char* c_str() const { return value_.c_str(); }
  size_t length() const { return value_.size(); }
  void toCharArray(char* output, size_t size) const {
    if (size) { std::strncpy(output, c_str(), size - 1); output[size - 1] = '\0'; }
  }
  bool operator!=(const String& other) const { return value_ != other.value_; }
  bool operator==(const String& other) const { return value_ == other.value_; }
 private:
  std::string value_;
};

struct Config { bool rotate180 = false; };
struct Preferences {
  std::map<std::string, std::vector<uint8_t>> blobs;
  std::map<std::string, bool> flags;
  std::map<std::string, std::string> strings;
  std::map<std::string, int> numbers;
  bool failOpen = false, failBlobWrite = false, failReadback = false, failClear = false;
  unsigned blobWrites = 0;
  bool begin(const char*, bool) { return !failOpen; }
  void end() {}
  bool isKey(const char* key) const { return blobs.count(key) != 0; }
  bool getBool(const char* key, bool fallback) const {
    const auto found = flags.find(key); return found == flags.end() ? fallback : found->second;
  }
  size_t putBool(const char* key, bool value) {
    if (failClear && !value) return 0;
    flags[key] = value; return 1;
  }
  size_t putBytes(const char* key, const void* data, size_t size) {
    if (failBlobWrite) return 0;
    const auto* first = static_cast<const uint8_t*>(data);
    blobs[key] = std::vector<uint8_t>(first, first + size); ++blobWrites; return size;
  }
  size_t getBytesLength(const char* key) const {
    const auto found = blobs.find(key); return found == blobs.end() ? 0 : found->second.size();
  }
  size_t getBytes(const char* key, void* output, size_t size) const {
    const auto found = blobs.find(key);
    if (failReadback || found == blobs.end()) return 0;
    const size_t count = std::min(size, found->second.size());
    std::memcpy(output, found->second.data(), count); return count;
  }
  String getString(const char* key, const char* fallback) const {
    const auto found = strings.find(key); return found == strings.end() ? String(fallback) : String(found->second);
  }
  uint8_t getUChar(const char* key, uint8_t fallback) const {
    const auto found = numbers.find(key); return found == numbers.end() ? fallback : found->second;
  }
  size_t putUChar(const char* key, uint8_t value) {
    if (failClear && value == 0) return 0;
    numbers[key] = value; return 1;
  }
  int16_t getShort(const char* key, int16_t fallback) const {
    const auto found = numbers.find(key); return found == numbers.end() ? fallback : found->second;
  }
} prefs;

struct FakePhotoPainter {
  std::string protectedSha, restoredSha;
  bool refreshFails = false, forceRefresh = false, cacheMissing = false;
  unsigned displays = 0;
  bool hardwareReady() { return true; }
  bool storageReady() { return true; }
  uint32_t lastRefreshDurationMs() { return 1; }
  uint8_t* allocateWireBuffer(size_t size) { return static_cast<uint8_t*>(std::malloc(size)); }
  bool readActiveSchedule(String&) { return false; }
  bool readStagedNextSchedule(String&) { return false; }
  const char* inFlightFormalFrameSha256() { return nullptr; }
  bool runFormalFrameGc(const char*, const char*, const char*, const char* lastGood,
                       const char*, const char*, const char*, inktime::DisplayRotation) {
    protectedSha = lastGood ? lastGood : ""; return true;
  }
  bool loadFormalFrame(const char* sha, inktime::DisplayRotation, uint8_t** frame) {
    if (cacheMissing) return false;
    restoredSha = sha; *frame = allocateWireBuffer(inktime::kPhotoPainterFrameBytes);
    if (*frame) std::memset(*frame, 0x33, inktime::kPhotoPainterFrameBytes);
    return *frame != nullptr;
  }
  bool displayFrame(uint8_t*, size_t) { ++displays; return !refreshFails; }
  const char* lastError() { return "simulated-refresh-failure"; }
  bool forceNetworkRefresh() { return forceRefresh; }
} photoPainter;

String currentPayloadSha256, currentReleaseId, currentRenderProfile;
String lastDeviceErrorCode, lastDeviceErrorMessage, lastDeviceWarningCode, lastDeviceWarningMessage;
bool offlineScheduleTxnBlocked = false, currentPayloadShaVerified = true, currentPayloadIntegrityTrusted = true;
bool deviceAuthInvalid = false;
inktime::ContentNotice pendingContentNotice = inktime::ContentNotice::None;
struct { uint32_t epd_transfer_ms = 0; } runtimeTelemetry;
uint32_t lastRefreshDurationMs = 0;
static void stopNetworkBeforeDisplay() {}
unsigned nvsWrites = 0;
void recordNvsWrite() { ++nvsWrites; }
void heap_caps_free(void* pointer) { std::free(pointer); }

/* SKETCH_FUNCTIONS */

int main() {
  Config cfg;
  const std::string a(64, 'a'), b(64, 'b');
  currentPayloadSha256 = a; currentReleaseId = "release-a"; currentRenderProfile = "safe_4c";
  assert(beginPanelMutation());
  saveDisplayRecord(cfg, true);
  const auto lastGoodA = prefs.blobs.at("disp_record");
  const auto successWrites = prefs.blobWrites;
  assert(loadDisplayRecord().valid && loadDisplayRecord().succeeded);
  assert(!prefs.getBool("disp_pending", true));
  assert(shouldSkipCurrentDisplay(cfg));

  // Actual sketch functions: A succeeds, B fails, then A remains recoverable and GC-protected.
  currentPayloadSha256 = b; currentReleaseId = "release-b";
  assert(beginPanelMutation());
  saveDisplayRecord(cfg, false);
  assert(prefs.blobs.at("disp_record") == lastGoodA && prefs.blobWrites == successWrites);
  assert(prefs.getBool("disp_pending", false));
  assert(runFormalFrameGcForWake() && photoPainter.protectedSha == a);
  assert(restoreLastSuccessfulPhoto() && photoPainter.restoredSha == a);
  assert(prefs.blobs.at("disp_record") == lastGoodA);

  // A failed refresh of the same SHA cannot become a false successful skip.
  currentPayloadSha256 = a; currentReleaseId = "release-a";
  saveDisplayRecord(cfg, false);
  assert(!shouldSkipCurrentDisplay(cfg));
  assert(prefs.blobs.at("disp_record") == lastGoodA);
  photoPainter.refreshFails = true;
  assert(!restoreLastSuccessfulPhoto());
  assert(prefs.getBool("disp_pending", false));
  assert(runFormalFrameGcForWake() && photoPainter.protectedSha == a);
  photoPainter.refreshFails = false;

  // A rejected NVS write preserves the previous success and its recovery anchor.
  currentPayloadSha256 = b; currentReleaseId = "release-b";
  prefs.failBlobWrite = true;
  saveDisplayRecord(cfg, true);
  assert(prefs.blobs.at("disp_record") == lastGoodA);
  assert(!shouldSkipCurrentDisplay(cfg));
  assert(runFormalFrameGcForWake() && photoPainter.protectedSha == a);
  prefs.failBlobWrite = false;

  // Readback or marker-clear failure keeps the panel uncertain despite a completed refresh.
  prefs.failReadback = true;
  saveDisplayRecord(cfg, true);
  assert(prefs.getBool("disp_pending", false));
  prefs.failReadback = false;
  prefs.failClear = true;
  saveDisplayRecord(cfg, true);
  assert(!shouldSkipCurrentDisplay(cfg));
  prefs.failClear = false;
  saveDisplayRecord(cfg, true);
  assert(shouldSkipCurrentDisplay(cfg));
  assert(runFormalFrameGcForWake() && photoPainter.protectedSha == b);

  // A shortage retains the photo anchor and draws only once across wake cycles.
  const auto lastGoodB = prefs.blobs.at("disp_record");
  pendingContentNotice = inktime::ContentNotice::AnalysisRequired;
  deviceAuthInvalid = true;
  assert(!displayContentNoticeSafely(cfg));
  deviceAuthInvalid = false;
  assert(displayContentNoticeSafely(cfg));
  const auto noticeDisplays = photoPainter.displays;
  const auto noticeWrites = nvsWrites;
  assert(prefs.blobs.at("disp_record") == lastGoodB);
  assert(!shouldSkipCurrentDisplay(cfg));
  assert(!displayContentNoticeSafely(cfg));
  assert(photoPainter.displays == noticeDisplays && nvsWrites == noticeWrites);
  assert(restoreLastSuccessfulPhoto());  // power-screen restoration retains the overlay
  assert(storedContentNotice() == inktime::ContentNotice::AnalysisRequired);
  saveDisplayRecord(cfg, true);  // next successful photo removes the notice
  assert(storedContentNotice() == inktime::ContentNotice::None);
  assert(shouldSkipCurrentDisplay(cfg));

  // Cache/refresh failure must not erase a known photo or claim notice success.
  photoPainter.cacheMissing = true;
  assert(!displayContentNoticeSafely(cfg));
  photoPainter.cacheMissing = false;
  photoPainter.refreshFails = true;
  assert(!displayContentNoticeSafely(cfg));
  assert(prefs.getBool("disp_pending", false));
  assert(prefs.blobs.at("disp_record") == lastGoodB);
  photoPainter.refreshFails = false;
  assert(displayContentNoticeSafely(cfg));
  assert(!prefs.getBool("disp_pending", true));
  saveDisplayRecord(cfg, true);

  // Legacy successful multi-key records remain readable for recovery after upgrade.
  prefs = Preferences{};
  prefs.numbers["disp_ver"] = 1; prefs.numbers["last_rot"] = 0; prefs.flags["last_ok"] = true;
  prefs.strings["last_sha"] = a; prefs.strings["last_rel"] = "legacy-a";
  prefs.strings["last_prof"] = "safe_4c"; prefs.strings["last_board"] = kBoardConfig.name;
  saveDisplayRecord(cfg, false);
  assert(!prefs.isKey("disp_record"));
  assert(loadDisplayRecord().valid && loadDisplayRecord().succeeded);
  assert(!shouldSkipCurrentDisplay(cfg));
  assert(runFormalFrameGcForWake() && photoPainter.protectedSha == a);
  assert(restoreLastSuccessfulPhoto() && photoPainter.restoredSha == a);
  return 0;
}
