#include "device_config_store_core.h"

#include <cassert>
#include <cstdint>
#include <map>
#include <string>

namespace {

using inktime::configstore::ConfigPayload;
using inktime::configstore::RecoveryJournal;

constexpr uint8_t kLegacyConfigSlots = 12U;

ConfigPayload payload(const std::string& suffix) {
  ConfigPayload value;
  value.wifi_ssid = "InkTime-" + suffix;
  value.wifi_pass = "secret-" + suffix;
  value.backend_hostport = "https://inktime.example.test:8765";
  value.ca_pem = "-----BEGIN CERTIFICATE-----\n" + suffix + "\n-----END CERTIFICATE-----";
  value.device_token = "token-" + suffix;
  value.device_secret = "secret-" + suffix;
  value.device_id = "esp32-" + suffix;
  value.auth_state = "paired";
  value.credential_version = 3;
  value.pairing_id = "pairing-" + suffix;
  value.pairing_nonce = "nonce-" + suffix + "-0123456789";
  value.pairing_expires_at_epoch = 1730000000ULL;
  value.pairing_retry_at_epoch = 1729999000ULL;
  value.pairing_retry_attempt = 2;
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
  value.sync_strategy = "fixed_daily";
  value.sync_time = "07:30";
  value.config_version = 42;
  return value;
}

std::string legacySchema3Payload(const ConfigPayload& value) {
  std::string output;
  using inktime::configstore::append_i32;
  using inktime::configstore::append_string;
  using inktime::configstore::append_u16;
  using inktime::configstore::append_u32;
  using inktime::configstore::append_u64;
  using inktime::configstore::append_u8;
  append_u8(output, 3U);
  assert(append_string(output, value.wifi_ssid, inktime::configstore::kMaxWifiSsidBytes));
  assert(append_string(output, value.wifi_pass, inktime::configstore::kMaxWifiPasswordBytes));
  assert(append_string(output, value.backend_hostport, inktime::configstore::kMaxBackendHostportBytes));
  assert(append_string(output, value.ca_pem, inktime::configstore::kMaxCaPemBytes));
  assert(append_string(output, value.device_token, inktime::configstore::kMaxDeviceTokenBytes));
  append_i32(output, value.tz_offset_minutes);
  append_u8(output, value.refresh_hour);
  append_u8(output, value.refresh_minute);
  append_u8(output, value.rotate180 ? 1U : 0U);
  append_u8(output, value.schedule_count);
  append_u16(output, value.prefetch_lead_minutes);
  assert(append_string(output, value.delivery_mode, inktime::configstore::kMaxDeliveryModeBytes));
  assert(append_string(output, value.button_wake_action, inktime::configstore::kMaxButtonWakeActionBytes));
  assert(append_string(output, value.device_secret, inktime::configstore::kMaxDeviceSecretBytes));
  assert(append_string(output, value.device_id, inktime::configstore::kMaxDeviceIdBytes));
  assert(append_string(output, value.auth_state, inktime::configstore::kMaxAuthStateBytes));
  assert(append_string(output, value.pairing_id, inktime::configstore::kMaxPairingIdBytes));
  assert(append_string(output, value.pairing_nonce, inktime::configstore::kMaxPairingNonceBytes));
  append_u32(output, value.config_version);
  append_u32(output, value.credential_version);
  append_u64(output, value.pairing_expires_at_epoch);
  append_u64(output, value.pairing_retry_at_epoch);
  append_u8(output, value.pairing_retry_attempt);
  for (uint8_t index = 0U; index < kLegacyConfigSlots; ++index) {
    append_u8(output, value.schedule_slots[index].hour);
    append_u8(output, value.schedule_slots[index].minute);
  }
  return output;
}

std::string legacySchema4Payload(const ConfigPayload& value) {
  std::string output;
  using inktime::configstore::append_i32;
  using inktime::configstore::append_string;
  using inktime::configstore::append_u16;
  using inktime::configstore::append_u32;
  using inktime::configstore::append_u64;
  using inktime::configstore::append_u8;
  append_u8(output, 4U);
  assert(append_string(output, value.wifi_ssid, inktime::configstore::kMaxWifiSsidBytes));
  assert(append_string(output, value.wifi_pass, inktime::configstore::kMaxWifiPasswordBytes));
  assert(append_string(output, value.backend_hostport, inktime::configstore::kMaxBackendHostportBytes));
  assert(append_string(output, value.ca_pem, inktime::configstore::kMaxCaPemBytes));
  assert(append_string(output, value.device_token, inktime::configstore::kMaxDeviceTokenBytes));
  append_i32(output, value.tz_offset_minutes);
  append_u8(output, value.refresh_hour);
  append_u8(output, value.refresh_minute);
  append_u8(output, value.rotate180 ? 1U : 0U);
  append_u8(output, value.schedule_count);
  append_u16(output, value.prefetch_lead_minutes);
  assert(append_string(output, value.delivery_mode, inktime::configstore::kMaxDeliveryModeBytes));
  assert(append_string(output, value.button_wake_action, inktime::configstore::kMaxButtonWakeActionBytes));
  assert(append_string(output, value.sync_strategy, inktime::configstore::kMaxSyncStrategyBytes));
  assert(append_string(output, value.sync_time, inktime::configstore::kMaxSyncTimeBytes));
  assert(append_string(output, value.device_secret, inktime::configstore::kMaxDeviceSecretBytes));
  assert(append_string(output, value.device_id, inktime::configstore::kMaxDeviceIdBytes));
  assert(append_string(output, value.auth_state, inktime::configstore::kMaxAuthStateBytes));
  assert(append_string(output, value.pairing_id, inktime::configstore::kMaxPairingIdBytes));
  assert(append_string(output, value.pairing_nonce, inktime::configstore::kMaxPairingNonceBytes));
  append_u32(output, value.config_version);
  append_u32(output, value.credential_version);
  append_u64(output, value.pairing_expires_at_epoch);
  append_u64(output, value.pairing_retry_at_epoch);
  append_u8(output, value.pairing_retry_attempt);
  for (uint8_t index = 0U; index < kLegacyConfigSlots; ++index) {
    append_u8(output, value.schedule_slots[index].hour);
    append_u8(output, value.schedule_slots[index].minute);
  }
  return output;
}

std::string legacySchema4Slot(const ConfigPayload& value, uint64_t generation) {
  const std::string serialized = legacySchema4Payload(value);
  std::string output;
  using inktime::configstore::append_u32;
  using inktime::configstore::append_u64;
  using inktime::configstore::append_u8;
  append_u32(output, inktime::configstore::kConfigSlotMagic);
  append_u8(output, inktime::configstore::kEnvelopeVersion);
  append_u8(output, 4U);
  append_u64(output, generation);
  append_u32(output, static_cast<uint32_t>(serialized.size()));
  append_u32(output, inktime::configstore::crc32(serialized));
  output.append(serialized);
  return output;
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

void rewriteCrc32(std::string& value) {
  assert(value.size() >= 4U);
  const uint32_t crc = inktime::configstore::crc32(value.substr(0U, value.size() - 4U));
  for (size_t index = 0U; index < 4U; ++index) {
    value[value.size() - 4U + index] = static_cast<char>((crc >> (index * 8U)) & 0xffU);
  }
}

bool selectCurrent(
    const std::map<std::string, std::string>& blobs,
    ConfigPayload& value,
    char& active_slot,
    uint64_t& generation) {
  active_slot = 0;
  generation = 0U;
  const auto pointer = blobs.find("active");
  if (pointer != blobs.end()) {
    std::string error;
    char pointed_slot = 0;
    uint64_t pointed_generation = 0U;
    ConfigPayload pointed;
    if (inktime::configstore::decode_pointer(
          pointer->second, pointed_slot, pointed_generation, error)
        && decodeSlot(blobs, pointed_slot, pointed, generation)
        && generation == pointed_generation) {
      value = pointed;
      active_slot = pointed_slot;
      return true;
    }
  }
  ConfigPayload candidate_a;
  ConfigPayload candidate_b;
  uint64_t generation_a = 0U;
  uint64_t generation_b = 0U;
  const bool valid_a = decodeSlot(blobs, 'A', candidate_a, generation_a);
  const bool valid_b = decodeSlot(blobs, 'B', candidate_b, generation_b);
  if (!valid_a && !valid_b) return false;
  if (valid_a && (!valid_b || generation_a <= generation_b)) {
    value = candidate_a;
    active_slot = 'A';
    generation = generation_a;
  } else {
    value = candidate_b;
    active_slot = 'B';
    generation = generation_b;
  }
  return true;
}

class FakeAbStore {
 public:
  std::map<std::string, std::string> blobs;
  std::string fail_next_put;
  std::string corrupt_next_put;

  bool put(const std::string& key, const std::string& value) {
    if (fail_next_put == key) {
      fail_next_put.clear();
      return false;
    }
    blobs[key] = value;
    if (corrupt_next_put == key) {
      corrupt_next_put.clear();
      blobs[key].back() ^= 0x01;
    }
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
  empty.wifi_ssid.clear();
  empty.wifi_pass.clear();
  empty.backend_hostport.clear();
  empty.ca_pem.clear();
  empty.device_token.clear();
  empty.device_secret.clear();
  empty.device_id.clear();
  empty.auth_state = "unpaired";
  empty.credential_version = 0;
  empty.pairing_id.clear();
  empty.pairing_nonce.clear();
  empty.pairing_expires_at_epoch = 0;
  empty.pairing_retry_at_epoch = 0;
  empty.pairing_retry_attempt = 0;
  assert(inktime::configstore::serialize_payload(empty, encoded, error));
  assert(inktime::configstore::deserialize_payload(encoded, decoded, error));
  assert(decoded == empty);
}

void test_legacy_schema_defaults_new_sync_policy_fields() {
  const ConfigPayload original = payload("legacy");
  const std::string encoded = legacySchema3Payload(original);
  ConfigPayload decoded;
  std::string error;
  assert(inktime::configstore::deserialize_payload(encoded, decoded, error));
  ConfigPayload expected = original;
  expected.sync_strategy = "first_display_lead";
  expected.sync_time.clear();
  assert(decoded == expected);
}

void test_schema4_12_slot_fixture_migrates_to_schema5() {
  const ConfigPayload original = payload("schema4");
  const std::string legacy_payload = legacySchema4Payload(original);
  ConfigPayload decoded;
  std::string error;
  assert(inktime::configstore::deserialize_payload(legacy_payload, decoded, error));
  assert(decoded == original);
  for (uint8_t index = kLegacyConfigSlots; index < inktime::configstore::kMaxConfigSlots; ++index) {
    assert(decoded.schedule_slots[index] == inktime::configstore::ScheduleSlot{});
  }

  const std::string legacy_slot = legacySchema4Slot(original, 21U);
  uint64_t generation = 0U;
  assert(inktime::configstore::decode_slot(legacy_slot, decoded, generation, error));
  assert(generation == 21U && decoded == original);

  std::string upgraded;
  assert(inktime::configstore::encode_slot(decoded, 22U, upgraded, error));
  assert(static_cast<uint8_t>(upgraded[5]) == 5U);
  ConfigPayload upgraded_value;
  assert(inktime::configstore::decode_slot(upgraded, upgraded_value, generation, error));
  assert(generation == 22U && upgraded_value == original);

  std::string truncated = legacy_slot;
  truncated.pop_back();
  assert(!inktime::configstore::decode_slot(truncated, upgraded_value, generation, error));
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
  corrupted[4] ^= 0x01;
  assert(!inktime::configstore::decode_slot(corrupted, decoded, generation, error));

  corrupted = encoded;
  // Schema 1 and 2 remain readable for existing A/B slots; reject an unknown schema.
  corrupted[5] = 0U;
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
  store.corrupt_next_put = "slot_b";
  assert(!store.save(second));
  assert(store.blobs.at("active") == first_pointer);
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

void test_pointer_selection_prefers_pointer_and_falls_back_to_oldest_safe_slot() {
  std::map<std::string, std::string> blobs;
  std::string error;
  assert(inktime::configstore::encode_slot(payload("a"), 4U, blobs["slot_a"], error));
  assert(inktime::configstore::encode_slot(payload("b"), 9U, blobs["slot_b"], error));

  assert(inktime::configstore::encode_pointer('A', 4U, blobs["active"], error));
  ConfigPayload current;
  char active_slot = 0;
  uint64_t generation = 0U;
  assert(selectCurrent(blobs, current, active_slot, generation));
  assert(active_slot == 'A' && generation == 4U && current == payload("a"));

  blobs["active"][0] ^= 0x01;
  assert(selectCurrent(blobs, current, active_slot, generation));
  assert(active_slot == 'A' && generation == 4U && current == payload("a"));

  blobs["slot_b"].back() ^= 0x01;
  assert(selectCurrent(blobs, current, active_slot, generation));
  assert(active_slot == 'A' && generation == 4U && current == payload("a"));

  blobs["slot_a"].back() ^= 0x01;
  assert(!selectCurrent(blobs, current, active_slot, generation));
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

  assert(inktime::configstore::encode_pointer('A', 16, pointer, error));
  pointer[5] = 'C';
  rewriteCrc32(pointer);
  assert(!inktime::configstore::decode_pointer(pointer, slot, generation, error));

  assert(inktime::configstore::encode_pointer('B', 17, pointer, error));
  pointer.pop_back();
  assert(!inktime::configstore::decode_pointer(pointer, slot, generation, error));

  for (const auto phase : {
      inktime::configstore::JournalPhase::Prepared,
      inktime::configstore::JournalPhase::SchedulePromoted,
      inktime::configstore::JournalPhase::ConfigCommitted,
      inktime::configstore::JournalPhase::Aborted,
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

    assert(inktime::configstore::encode_journal(journal, encoded, error));
    encoded[4] ^= 0x01;
    assert(!inktime::configstore::decode_journal(encoded, decoded, error));

    assert(inktime::configstore::encode_journal(journal, encoded, error));
    encoded[5] = 0U;
    rewriteCrc32(encoded);
    assert(!inktime::configstore::decode_journal(encoded, decoded, error));

    assert(inktime::configstore::encode_journal(journal, encoded, error));
    encoded.push_back('\0');
    assert(!inktime::configstore::decode_journal(encoded, decoded, error));
  }
}

}  // namespace

int main() {
  test_payload_roundtrip_and_empty_overwrite();
  test_legacy_schema_defaults_new_sync_policy_fields();
  test_schema4_12_slot_fixture_migrates_to_schema5();
  test_envelope_rejects_corruption_and_shape_changes();
  test_ab_pointer_and_write_failure_injection();
  test_pointer_selection_prefers_pointer_and_falls_back_to_oldest_safe_slot();
  test_pointer_and_journal_roundtrip();
  return 0;
}
