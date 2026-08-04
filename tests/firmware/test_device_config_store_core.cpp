#include "device_config_store_core.h"

#include <cassert>
#include <cstdint>
#include <map>
#include <string>

namespace {

using inktime::configstore::ConfigPayload;
using inktime::configstore::RecoveryJournal;

ConfigPayload payload(const std::string& suffix) {
  ConfigPayload value;
  value.wifi_ssid = "InkTime-" + suffix;
  value.wifi_pass = "secret-" + suffix;
  value.backend_hostport = "https://inktime.example.test:8765";
  value.ca_pem = "-----BEGIN CERTIFICATE-----\n" + suffix + "\n-----END CERTIFICATE-----";
  value.device_token = "token-" + suffix;
  value.tz_offset_minutes = 480;
  value.refresh_hour = 8;
  value.refresh_minute = 30;
  value.rotate180 = true;
  value.schedule_count = 4;
  value.schedule_slots[0] = {6, 0};
  value.schedule_slots[1] = {8, 30};
  value.schedule_slots[2] = {12, 5};
  value.schedule_slots[3] = {22, 45};
  value.prefetch_lead_minutes = 17;
  value.delivery_mode = "inktime_offline_schedule";
  value.button_wake_action = "local_next";
  value.config_version = 42;
  return value;
}

bool decodeSlot(
    const std::map<std::string, std::string>& blobs,
    char slot,
    ConfigPayload& value,
    uint64_t& generation) {
  const auto found = blobs.find(slot == 'A' ? "slot_a" : "slot_b");
  if (found == blobs.end()) return false;
  std::string error;
  return inktime::configstore::decode_slot(found->second, value, generation, error);
}

class FakeAbStore {
 public:
  std::map<std::string, std::string> blobs;
  std::string fail_next_put;

  bool put(const std::string& key, const std::string& value) {
    if (fail_next_put == key) {
      fail_next_put.clear();
      return false;
    }
    blobs[key] = value;
    return true;
  }

  bool save(const ConfigPayload& value) {
    char active_slot = 0;
    uint64_t active_generation = 0;
    ConfigPayload active;
    std::string pointer_error;
    const auto pointer = blobs.find("active");
    const bool had_pointer = pointer != blobs.end();
    if (pointer != blobs.end()
        && !inktime::configstore::decode_pointer(
          pointer->second, active_slot, active_generation, pointer_error)) {
      return false;
    }
    if (active_slot != 0 && !decodeSlot(blobs, active_slot, active, active_generation)) return false;
    const char prepared_slot = active_slot == 'A' ? 'B' : 'A';
    const uint64_t prepared_generation = active_slot == 0 ? 1 : active_generation + 1;
    std::string envelope;
    std::string error;
    if (!inktime::configstore::encode_slot(value, prepared_generation, envelope, error)
        || !put(prepared_slot == 'A' ? "slot_a" : "slot_b", envelope)) {
      return false;
    }
    ConfigPayload readback;
    uint64_t readback_generation = 0;
    if (!decodeSlot(blobs, prepared_slot, readback, readback_generation)
        || readback_generation != prepared_generation || !(readback == value)) {
      return false;
    }
    std::string pointer_blob;
    if (!inktime::configstore::encode_pointer(
          prepared_slot, prepared_generation, pointer_blob, error)) return false;
    const std::string old_pointer = had_pointer ? pointer->second : std::string();
    if (!put("active", pointer_blob)) return false;
    char verified_slot = 0;
    uint64_t verified_generation = 0;
    const auto written_pointer = blobs.find("active");
    if (written_pointer == blobs.end()
        || !inktime::configstore::decode_pointer(
          written_pointer->second, verified_slot, verified_generation, error)
        || verified_slot != prepared_slot || verified_generation != prepared_generation) {
      if (old_pointer.empty()) blobs.erase("active");
      else blobs["active"] = old_pointer;
      return false;
    }
    return true;
  }
};

void test_payload_roundtrip_and_empty_overwrite() {
  ConfigPayload original = payload("roundtrip");
  std::string encoded;
  std::string error;
  assert(inktime::configstore::serialize_payload(original, encoded, error));
  ConfigPayload decoded;
  assert(inktime::configstore::deserialize_payload(encoded, decoded, error));
  assert(decoded == original);

  ConfigPayload empty = original;
  empty.wifi_pass.clear();
  empty.ca_pem.clear();
  empty.device_token.clear();
  assert(inktime::configstore::serialize_payload(empty, encoded, error));
  assert(inktime::configstore::deserialize_payload(encoded, decoded, error));
  assert(decoded == empty);
}

void test_envelope_rejects_corruption_and_shape_changes() {
  ConfigPayload value = payload("envelope");
  std::string encoded;
  std::string error;
  assert(inktime::configstore::encode_slot(value, 9, encoded, error));
  ConfigPayload decoded;
  uint64_t generation = 0;
  assert(inktime::configstore::decode_slot(encoded, decoded, generation, error));
  assert(generation == 9 && decoded == value);

  std::string corrupted = encoded;
  corrupted.back() ^= 0x01;
  assert(!inktime::configstore::decode_slot(corrupted, decoded, generation, error));

  corrupted = encoded;
  corrupted[0] ^= 0x01;
  assert(!inktime::configstore::decode_slot(corrupted, decoded, generation, error));

  corrupted = encoded;
  corrupted.resize(corrupted.size() - 1);
  assert(!inktime::configstore::decode_slot(corrupted, decoded, generation, error));

  corrupted = encoded;
  corrupted.push_back('\0');
  assert(!inktime::configstore::decode_slot(corrupted, decoded, generation, error));

  corrupted = encoded;
  corrupted[14] = static_cast<char>(0xff);
  assert(!inktime::configstore::decode_slot(corrupted, decoded, generation, error));
}

void test_ab_pointer_and_write_failure_injection() {
  FakeAbStore store;
  const ConfigPayload first = payload("a");
  const ConfigPayload second = payload("b");
  const ConfigPayload third = payload("c");
  assert(store.save(first));
  const std::string first_pointer = store.blobs.at("active");
  store.fail_next_put = "slot_b";
  assert(!store.save(second));
  assert(store.blobs.at("active") == first_pointer);
  assert(store.save(second));
  const std::string second_pointer = store.blobs.at("active");
  char active_slot = 0;
  uint64_t generation = 0;
  std::string error;
  assert(inktime::configstore::decode_pointer(
    second_pointer, active_slot, generation, error));
  assert(active_slot == 'B' && generation == 2);
  store.fail_next_put = "active";
  assert(!store.save(third));
  assert(store.blobs.at("active") == second_pointer);
  ConfigPayload active;
  uint64_t active_generation = 0;
  assert(decodeSlot(store.blobs, 'B', active, active_generation));
  assert(active == second && active_generation == 2);
}

void test_pointer_and_journal_roundtrip() {
  std::string error;
  std::string pointer;
  assert(inktime::configstore::encode_pointer('B', 17, pointer, error));
  char slot = 0;
  uint64_t generation = 0;
  assert(inktime::configstore::decode_pointer(pointer, slot, generation, error));
  assert(slot == 'B' && generation == 17);
  pointer[7] ^= 0x01;
  assert(!inktime::configstore::decode_pointer(pointer, slot, generation, error));

  for (const auto phase : {
      inktime::configstore::JournalPhase::Prepared,
      inktime::configstore::JournalPhase::SchedulePromoted,
      inktime::configstore::JournalPhase::ConfigCommitted,
  }) {
    RecoveryJournal journal;
    journal.phase = phase;
    journal.target_schedule_id = "schedule-2026-08-04";
    journal.previous_active_slot = 'A';
    journal.previous_generation = 11;
    journal.prepared_slot = 'B';
    journal.prepared_generation = 12;
    std::string encoded;
    assert(inktime::configstore::encode_journal(journal, encoded, error));
    RecoveryJournal decoded;
    assert(inktime::configstore::decode_journal(encoded, decoded, error));
    assert(decoded.phase == phase);
    assert(decoded.target_schedule_id == journal.target_schedule_id);
    assert(decoded.prepared_generation == 12);
    encoded[encoded.size() - 1] ^= 0x01;
    assert(!inktime::configstore::decode_journal(encoded, decoded, error));
  }
}

}  // namespace

int main() {
  test_payload_roundtrip_and_empty_overwrite();
  test_envelope_rejects_corruption_and_shape_changes();
  test_ab_pointer_and_write_failure_injection();
  test_pointer_and_journal_roundtrip();
  return 0;
}
