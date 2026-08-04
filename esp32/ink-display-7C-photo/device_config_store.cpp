#include "device_config_store.h"

#include <algorithm>
#include <limits>

namespace inktime {

namespace {

constexpr const char* kSlotAKey = "slot_a";
constexpr const char* kSlotBKey = "slot_b";
constexpr const char* kPointerKey = "active";
constexpr const char* kJournalKey = "sched_txn";
constexpr const char* kLegacyJournalKey = "journal";

const char* slotKey(char slot) {
  return slot == 'A' ? kSlotAKey : kSlotBKey;
}

bool hasLegacyKey(Preferences& store, const char* key) {
  return store.isKey(key);
}

}  // namespace

void DeviceConfigStore::setError(String& error, const char* value) {
  error = value == nullptr ? "PAIRING-NVS-UNKNOWN" : value;
}

bool DeviceConfigStore::readBlob(
    Preferences& store,
    const char* key,
    std::string& bytes,
    bool& present,
    String& error) const {
  bytes.clear();
  present = false;
  const size_t length = store.getBytesLength(key);
  if (length == 0U) return true;
  if (length > configstore::kMaxConfigPayloadBytes + 64U) {
    setError(error, "PAIRING-NVS-004");
    return false;
  }
  bytes.resize(length);
  if (store.getBytes(key, &bytes[0], length) != length) {
    bytes.clear();
    setError(error, "PAIRING-NVS-004");
    return false;
  }
  present = true;
  return true;
}

bool DeviceConfigStore::readSlot(
    Preferences& store,
    char slot,
    SlotValue& value,
    String& error) const {
  if (!configstore::valid_slot_name(slot)) {
    setError(error, "PAIRING-NVS-004");
    return false;
  }
  std::string bytes;
  bool present = false;
  if (!readBlob(store, slotKey(slot), bytes, present, error) || !present) return false;
  value.slot = slot;
  std::string core_error;
  if (!configstore::decode_slot(bytes, value.payload, value.generation, core_error)) {
    setError(error, core_error.c_str());
    return false;
  }
  return true;
}

bool DeviceConfigStore::readPointer(
    Preferences& store,
    char& active_slot,
    uint64_t& generation,
    bool& present,
    String& error) const {
  active_slot = 0;
  generation = 0U;
  std::string bytes;
  if (!readBlob(store, kPointerKey, bytes, present, error) || !present) return true;
  std::string core_error;
  if (!configstore::decode_pointer(bytes, active_slot, generation, core_error)) {
    setError(error, "PAIRING-NVS-005");
    return false;
  }
  return true;
}

bool DeviceConfigStore::writePointer(
    Preferences& store,
    char active_slot,
    uint64_t generation,
    String& error) const {
  std::string bytes;
  std::string core_error;
  if (!configstore::encode_pointer(active_slot, generation, bytes, core_error)) {
    setError(error, "PAIRING-NVS-005");
    return false;
  }
  if (store.putBytes(kPointerKey, bytes.data(), bytes.size()) != bytes.size()) {
    setError(error, "PAIRING-NVS-005");
    return false;
  }
  return true;
}

bool DeviceConfigStore::findNewest(
    Preferences& store,
    SlotValue& value,
    String& error) const {
  SlotValue candidateA;
  SlotValue candidateB;
  String ignoredA;
  String ignoredB;
  const bool validA = readSlot(store, 'A', candidateA, ignoredA);
  const bool validB = readSlot(store, 'B', candidateB, ignoredB);
  if (!validA && !validB) {
    setError(error, "");
    return false;
  }
  if (validA && (!validB || candidateA.generation >= candidateB.generation)) {
    value = candidateA;
  } else {
    value = candidateB;
  }
  return true;
}

bool DeviceConfigStore::readCurrent(
    Preferences& store,
    SlotValue& value,
    bool& present,
    String& error) const {
  present = false;
  char pointer_slot = 0;
  uint64_t pointer_generation = 0U;
  bool pointer_present = false;
  String pointer_error;
  if (!readPointer(store, pointer_slot, pointer_generation, pointer_present, pointer_error)) {
    // A damaged pointer is recoverable if one complete A/B slot remains.
    if (!findNewest(store, value, error)) return true;
    present = true;
    return true;
  }
  if (pointer_present) {
    SlotValue pointed;
    String slot_error;
    if (readSlot(store, pointer_slot, pointed, slot_error)
        && pointed.generation == pointer_generation) {
      value = pointed;
      present = true;
      return true;
    }
  }
  if (findNewest(store, value, error)) {
    present = true;
    return true;
  }
  return true;
}

bool DeviceConfigStore::parseLegacyClock(
    const String& value,
    configstore::ScheduleSlot& slot) {
  if (value.length() != 5U || value[2] != ':') return false;
  if (value[0] < '0' || value[0] > '9' || value[1] < '0' || value[1] > '9'
      || value[3] < '0' || value[3] > '9' || value[4] < '0' || value[4] > '9') {
    return false;
  }
  slot.hour = static_cast<uint8_t>((value[0] - '0') * 10 + value[1] - '0');
  slot.minute = static_cast<uint8_t>((value[3] - '0') * 10 + value[4] - '0');
  return configstore::valid_slot(slot);
}

bool DeviceConfigStore::loadLegacy(
    configstore::ConfigPayload& payload,
    bool& present,
    String& error) const {
  present = false;
  Preferences legacy;
  if (!legacy.begin("dashcfg", true)) {
    setError(error, "PAIRING-NVS-002");
    return false;
  }
  const char* formalKeys[] = {
    "ssid", "pass", "hostport", "ca_pem", "devtoken", "tzmin", "tz",
    "hour", "minute", "rot180", "prefetch", "delivery", "button", "cfgver", "scnt",
  };
  for (const char* key : formalKeys) {
    if (hasLegacyKey(legacy, key)) {
      present = true;
      break;
    }
  }
  if (!present) {
    legacy.end();
    return true;
  }

  payload = configstore::ConfigPayload();
  payload.wifi_ssid = legacy.getString("ssid", "").c_str();
  payload.wifi_pass = legacy.getString("pass", "").c_str();
  payload.backend_hostport = legacy.getString("hostport", "").c_str();
  payload.ca_pem = legacy.getString("ca_pem", "").c_str();
  payload.device_token = legacy.getString("devtoken", "").c_str();
  payload.tz_offset_minutes = legacy.getInt("tzmin", legacy.getInt("tz", 8) * 60);
  payload.refresh_hour = legacy.getUChar("hour", 8U);
  payload.refresh_minute = legacy.getUChar("minute", 0U);
  payload.rotate180 = legacy.getBool("rot180", false);
  payload.prefetch_lead_minutes = legacy.getUShort("prefetch", 5U);
  payload.delivery_mode = legacy.getString("delivery", "legacy_online").c_str();
  payload.button_wake_action = legacy.getString("button", "check_new").c_str();
  payload.config_version = legacy.getULong("cfgver", 0U);
  payload.schedule_count = 1U;
  payload.schedule_slots[0] = {payload.refresh_hour, payload.refresh_minute};
  const uint8_t count = legacy.getUChar("scnt", 0U);
  if (count > 0U && count <= configstore::kMaxConfigSlots) {
    configstore::ScheduleSlot slots[configstore::kMaxConfigSlots] = {};
    bool valid = true;
    for (uint8_t index = 0U; index < count; ++index) {
      const String key = String("s") + String(index);
      if (!parseLegacyClock(legacy.getString(key.c_str(), ""), slots[index])) {
        valid = false;
        break;
      }
    }
    if (valid) {
      configstore::ConfigPayload candidate = payload;
      candidate.schedule_count = count;
      for (uint8_t index = 0U; index < count; ++index) {
        candidate.schedule_slots[index] = slots[index];
      }
      candidate.refresh_hour = slots[0].hour;
      candidate.refresh_minute = slots[0].minute;
      std::string candidate_error;
      if (configstore::validate_payload(candidate, candidate_error)) payload = candidate;
    }
  }
  for (uint8_t index = payload.schedule_count; index < configstore::kMaxConfigSlots; ++index) {
    payload.schedule_slots[index] = {0U, 0U};
  }
  legacy.end();
  std::string core_error;
  if (!configstore::validate_payload(payload, core_error)) {
    setError(error, "PAIRING-NVS-001");
    return false;
  }
  return true;
}

bool DeviceConfigStore::removeLegacyFormalKeys(String& error) const {
  Preferences legacy;
  if (!legacy.begin("dashcfg", false)) {
    setError(error, "PAIRING-NVS-002");
    return false;
  }
  const char* formalKeys[] = {
    "ssid", "pass", "hostport", "ca_pem", "devtoken", "tzmin", "tz", "hour", "minute",
    "rot180", "prefetch", "delivery", "button", "cfgver", "scnt",
  };
  for (const char* key : formalKeys) legacy.remove(key);
  for (uint8_t index = 0U; index < configstore::kMaxConfigSlots; ++index) {
    const String key = String("s") + String(index);
    legacy.remove(key.c_str());
  }
  legacy.end();
  return true;
}

bool DeviceConfigStore::load(configstore::ConfigPayload& payload, String& error) {
  error = "";
  Preferences store;
  if (!store.begin(storage_namespace_, false)) {
    setError(error, "PAIRING-NVS-002");
    return false;
  }
  SlotValue current;
  bool current_present = false;
  String current_error;
  const bool current_ok = readCurrent(store, current, current_present, current_error);
  if (current_ok && current_present) {
    char pointer_slot = 0;
    uint64_t pointer_generation = 0U;
    bool pointer_present = false;
    String pointer_error;
    const bool pointer_ok = readPointer(
      store, pointer_slot, pointer_generation, pointer_present, pointer_error);
    if (!pointer_ok || !pointer_present || pointer_slot != current.slot
        || pointer_generation != current.generation) {
      String repair_error;
      if (!writePointer(store, current.slot, current.generation, repair_error)) {
        store.end();
        setError(error, "PAIRING-NVS-005");
        return false;
      }
    }
    payload = current.payload;
    store.end();
    return true;
  }
  store.end();

  bool legacy_present = false;
  if (!loadLegacy(payload, legacy_present, error)) return false;
  if (!legacy_present) return false;

  String migration_error;
  if (save(payload, migration_error)) {
    String cleanup_error;
    if (!removeLegacyFormalKeys(cleanup_error) && error.length() == 0) error = cleanup_error;
  }
  // Migration remains usable even when the new blob cannot be committed; the
  // next boot retries it and the legacy keys are never deleted prematurely.
  return true;
}

bool DeviceConfigStore::prepare(
    const configstore::ConfigPayload& payload,
    Prepared& prepared,
    String& error) {
  error = "";
  std::string encoded;
  std::string core_error;
  if (!configstore::encode_slot(payload, 1U, encoded, core_error)) {
    setError(error, core_error.c_str());
    return false;
  }
  Preferences store;
  if (!store.begin(storage_namespace_, false)) {
    setError(error, "PAIRING-NVS-002");
    return false;
  }
  SlotValue current;
  bool current_present = false;
  String current_error;
  if (!readCurrent(store, current, current_present, current_error)) {
    store.end();
    setError(error, "PAIRING-NVS-004");
    return false;
  }
  prepared.previous_active_slot = current_present ? current.slot : 0;
  prepared.previous_generation = current_present ? current.generation : 0U;
  prepared.prepared_slot = current_present && current.slot == 'A' ? 'B' : 'A';
  if (current_present && current.generation == std::numeric_limits<uint64_t>::max()) {
    store.end();
    setError(error, "PAIRING-NVS-001");
    return false;
  }
  prepared.prepared_generation = current_present ? current.generation + 1U : 1U;
  encoded.clear();
  if (!configstore::encode_slot(payload, prepared.prepared_generation, encoded, core_error)
      || store.putBytes(
        slotKey(prepared.prepared_slot), encoded.data(), encoded.size()) != encoded.size()) {
    store.end();
    setError(error, "PAIRING-NVS-001");
    return false;
  }
  store.end();

  Preferences verify;
  if (!verify.begin(storage_namespace_, true)) {
    setError(error, "PAIRING-NVS-002");
    return false;
  }
  SlotValue readback;
  String readback_error;
  const bool readback_ok = readSlot(verify, prepared.prepared_slot, readback, readback_error)
    && readback.generation == prepared.prepared_generation
    && readback.payload == payload;
  verify.end();
  if (!readback_ok) {
    setError(error, "PAIRING-NVS-003");
    return false;
  }
  prepared.payload = payload;
  return true;
}

bool DeviceConfigStore::commit(const Prepared& prepared, String& error) {
  return commitPreparedSlot(
      prepared.prepared_slot,
      prepared.prepared_generation,
      prepared.payload,
      error);
}

bool DeviceConfigStore::commitPreparedSlot(
    char prepared_slot,
    uint64_t prepared_generation,
    const configstore::ConfigPayload& payload,
    String& error) {
  error = "";
  Preferences store;
  if (!store.begin(storage_namespace_, false)) {
    setError(error, "PAIRING-NVS-002");
    return false;
  }
  SlotValue prepared;
  String prepared_error;
  const bool slot_ok = readSlot(store, prepared_slot, prepared, prepared_error)
    && prepared.generation == prepared_generation
    && prepared.payload == payload;
  if (!slot_ok) {
    store.end();
    setError(error, "PAIRING-NVS-003");
    return false;
  }
  char previous_slot = 0;
  uint64_t previous_generation = 0U;
  bool previous_present = false;
  String pointer_error;
  if (!readPointer(store, previous_slot, previous_generation, previous_present, pointer_error)) {
    previous_present = false;
  }

  const auto restorePreviousPointer = [&]() {
    Preferences restore;
    if (!restore.begin(storage_namespace_, false)) return;
    if (previous_present) {
      String restore_error;
      (void)writePointer(restore, previous_slot, previous_generation, restore_error);
    } else {
      restore.remove(kPointerKey);
    }
    restore.end();
  };

  if (!writePointer(store, prepared_slot, prepared_generation, error)) {
    store.end();
    restorePreviousPointer();
    return false;
  }
  store.end();

  Preferences verify;
  bool active_ok = false;
  if (verify.begin(storage_namespace_, true)) {
    char read_slot_name = 0;
    uint64_t read_generation = 0U;
    bool read_present = false;
    String read_error;
    const bool pointer_ok = readPointer(
        verify, read_slot_name, read_generation, read_present, read_error)
      && read_present && read_slot_name == prepared_slot
      && read_generation == prepared_generation;
    SlotValue active;
    active_ok = pointer_ok && readSlot(verify, prepared_slot, active, read_error)
      && active.generation == prepared_generation && active.payload == payload;
    verify.end();
  }
  if (!active_ok) {
    restorePreviousPointer();
    setError(error, "PAIRING-NVS-005");
    return false;
  }
  return true;
}

bool DeviceConfigStore::save(const configstore::ConfigPayload& payload, String& error) {
  Prepared prepared;
  if (!prepare(payload, prepared, error)) return false;
  return commit(prepared, error);
}

bool DeviceConfigStore::readActive(
    configstore::ConfigPayload& payload,
    char& active_slot,
    uint64_t& generation,
    String& error) {
  error = "";
  Preferences store;
  if (!store.begin(storage_namespace_, false)) {
    setError(error, "PAIRING-NVS-002");
    return false;
  }
  SlotValue current;
  bool present = false;
  if (!readCurrent(store, current, present, error) || !present) {
    store.end();
    return false;
  }
  payload = current.payload;
  active_slot = current.slot;
  generation = current.generation;
  store.end();
  return true;
}

bool DeviceConfigStore::readPrepared(
    char prepared_slot,
    uint64_t prepared_generation,
    configstore::ConfigPayload& payload,
    String& error) {
  error = "";
  Preferences store;
  if (!store.begin(storage_namespace_, false)) {
    setError(error, "PAIRING-NVS-002");
    return false;
  }
  SlotValue value;
  const bool ok = readSlot(store, prepared_slot, value, error)
    && value.generation == prepared_generation;
  if (ok) payload = value.payload;
  store.end();
  if (!ok && error.length() == 0U) setError(error, "PAIRING-NVS-003");
  return ok;
}

bool DeviceConfigStore::readJournal(
    configstore::RecoveryJournal& journal,
    bool& present,
    String& error) {
  error = "";
  present = false;
  Preferences store;
  if (!store.begin(storage_namespace_, false)) {
    setError(error, "PAIRING-NVS-002");
    return false;
  }
  std::string bytes;
  if (!readBlob(store, kJournalKey, bytes, present, error)) {
    store.end();
    return false;
  }
  if (!present) {
    String legacy_error;
    if (!readBlob(store, kLegacyJournalKey, bytes, present, legacy_error)) {
      store.end();
      setError(error, legacy_error.length() > 0U ? legacy_error.c_str() : "PAIRING-NVS-005");
      return false;
    }
  }
  if (!present) {
    store.end();
    return true;
  }
  std::string core_error;
  const bool ok = configstore::decode_journal(bytes, journal, core_error);
  store.end();
  if (!ok) setError(error, "PAIRING-NVS-005");
  return ok;
}

bool DeviceConfigStore::writeJournal(
    const configstore::RecoveryJournal& journal,
    String& error) {
  error = "";
  std::string bytes;
  std::string core_error;
  if (!configstore::encode_journal(journal, bytes, core_error)) {
    setError(error, "PAIRING-NVS-005");
    return false;
  }
  Preferences store;
  if (!store.begin(storage_namespace_, false)) {
    setError(error, "PAIRING-NVS-002");
    return false;
  }
  const bool ok = store.putBytes(kJournalKey, bytes.data(), bytes.size()) == bytes.size();
  if (ok) store.remove(kLegacyJournalKey);
  store.end();
  if (!ok) setError(error, "PAIRING-NVS-005");
  return ok;
}

bool DeviceConfigStore::clearJournal(String& error) {
  error = "";
  Preferences store;
  if (!store.begin(storage_namespace_, false)) {
    setError(error, "PAIRING-NVS-002");
    return false;
  }
  store.remove(kJournalKey);
  store.remove(kLegacyJournalKey);
  store.end();
  return true;
}

bool DeviceConfigStore::clearAll(String& error) {
  error = "";
  Preferences store;
  if (!store.begin(storage_namespace_, false)) {
    setError(error, "PAIRING-NVS-002");
    return false;
  }
  store.clear();
  store.end();
  return true;
}

}  // namespace inktime
