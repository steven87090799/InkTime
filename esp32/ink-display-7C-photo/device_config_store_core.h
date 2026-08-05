#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace inktime {
namespace configstore {

constexpr uint8_t kMaxConfigSlots = 12U;
constexpr size_t kMaxConfigPayloadBytes = 8192U;
constexpr size_t kMaxWifiSsidBytes = 64U;
constexpr size_t kMaxWifiPasswordBytes = 128U;
constexpr size_t kMaxBackendHostportBytes = 512U;
constexpr size_t kMaxCaPemBytes = 3500U;
constexpr size_t kMaxDeviceTokenBytes = 1024U;
constexpr size_t kMaxDeviceSecretBytes = 256U;
constexpr size_t kMaxDeviceIdBytes = 128U;
constexpr size_t kMaxAuthStateBytes = 32U;
constexpr size_t kMaxPairingIdBytes = 128U;
constexpr size_t kMaxPairingNonceBytes = 256U;
constexpr size_t kMaxDeliveryModeBytes = 64U;
constexpr size_t kMaxButtonWakeActionBytes = 64U;
constexpr size_t kMaxScheduleIdBytes = 160U;

constexpr uint32_t kConfigSlotMagic = 0x494E4B43U;  // INKC
constexpr uint32_t kConfigPointerMagic = 0x494E4B50U;  // INKP
constexpr uint32_t kScheduleJournalMagic = 0x494E4B4AU;  // INKJ
constexpr uint8_t kEnvelopeVersion = 1U;
constexpr uint8_t kPayloadSchemaVersion = 3U;
constexpr uint8_t kPointerVersion = 1U;
constexpr uint8_t kJournalVersion = 1U;
constexpr const char* kGenericCommitTargetScheduleId = "__config_commit__";

struct ScheduleSlot {
  uint8_t hour = 0U;
  uint8_t minute = 0U;
};

struct ConfigPayload {
  std::string wifi_ssid;
  std::string wifi_pass;
  std::string backend_hostport;
  std::string ca_pem;
  std::string device_token;
  std::string device_secret;
  std::string device_id;
  std::string auth_state = "unpaired";
  uint32_t credential_version = 0U;
  std::string pairing_id;
  std::string pairing_nonce;
  uint64_t pairing_expires_at_epoch = 0U;
  uint64_t pairing_retry_at_epoch = 0U;
  uint8_t pairing_retry_attempt = 0U;
  int32_t tz_offset_minutes = 8 * 60;
  uint8_t refresh_hour = 8U;
  uint8_t refresh_minute = 0U;
  bool rotate180 = false;
  ScheduleSlot schedule_slots[kMaxConfigSlots] = {};
  uint8_t schedule_count = 1U;
  uint16_t prefetch_lead_minutes = 5U;
  std::string delivery_mode = "legacy_online";
  std::string button_wake_action = "check_new";
  uint32_t config_version = 0U;
};

enum class JournalPhase : uint8_t {
  None = 0U,
  Prepared = 1U,
  SchedulePromoted = 2U,
  ConfigCommitted = 3U,
  Aborted = 4U,
};

struct RecoveryJournal {
  JournalPhase phase = JournalPhase::None;
  std::string target_schedule_id;
  char previous_active_slot = 0;
  uint64_t previous_generation = 0U;
  char prepared_slot = 0;
  uint64_t prepared_generation = 0U;
};

inline bool operator==(const ScheduleSlot& left, const ScheduleSlot& right) {
  return left.hour == right.hour && left.minute == right.minute;
}

inline bool operator==(const ConfigPayload& left, const ConfigPayload& right) {
  if (left.wifi_ssid != right.wifi_ssid
      || left.wifi_pass != right.wifi_pass
      || left.backend_hostport != right.backend_hostport
      || left.ca_pem != right.ca_pem
      || left.device_token != right.device_token
      || left.device_secret != right.device_secret
      || left.device_id != right.device_id
      || left.auth_state != right.auth_state
      || left.credential_version != right.credential_version
      || left.pairing_id != right.pairing_id
      || left.pairing_nonce != right.pairing_nonce
      || left.pairing_expires_at_epoch != right.pairing_expires_at_epoch
      || left.pairing_retry_at_epoch != right.pairing_retry_at_epoch
      || left.pairing_retry_attempt != right.pairing_retry_attempt
      || left.tz_offset_minutes != right.tz_offset_minutes
      || left.refresh_hour != right.refresh_hour
      || left.refresh_minute != right.refresh_minute
      || left.rotate180 != right.rotate180
      || left.schedule_count != right.schedule_count
      || left.prefetch_lead_minutes != right.prefetch_lead_minutes
      || left.delivery_mode != right.delivery_mode
      || left.button_wake_action != right.button_wake_action
      || left.config_version != right.config_version) {
    return false;
  }
  for (uint8_t index = 0U; index < kMaxConfigSlots; ++index) {
    if (!(left.schedule_slots[index] == right.schedule_slots[index])) return false;
  }
  return true;
}

inline void set_error(std::string& error, const char* value) {
  error = value == nullptr ? "config store error" : value;
}

inline void append_u8(std::string& output, uint8_t value) {
  output.push_back(static_cast<char>(value));
}

inline void append_u16(std::string& output, uint16_t value) {
  append_u8(output, static_cast<uint8_t>(value & 0xffU));
  append_u8(output, static_cast<uint8_t>((value >> 8U) & 0xffU));
}

inline void append_u32(std::string& output, uint32_t value) {
  for (uint8_t shift = 0U; shift < 32U; shift += 8U) {
    append_u8(output, static_cast<uint8_t>((value >> shift) & 0xffU));
  }
}

inline void append_u64(std::string& output, uint64_t value) {
  for (uint8_t shift = 0U; shift < 64U; shift += 8U) {
    append_u8(output, static_cast<uint8_t>((value >> shift) & 0xffU));
  }
}

inline void append_i32(std::string& output, int32_t value) {
  append_u32(output, static_cast<uint32_t>(value));
}

inline bool take_u8(const std::string& input, size_t& offset, uint8_t& value) {
  if (offset >= input.size()) return false;
  value = static_cast<uint8_t>(input[offset++]);
  return true;
}

inline bool take_u16(const std::string& input, size_t& offset, uint16_t& value) {
  uint8_t first = 0U;
  uint8_t second = 0U;
  if (!take_u8(input, offset, first) || !take_u8(input, offset, second)) return false;
  value = static_cast<uint16_t>(first) | (static_cast<uint16_t>(second) << 8U);
  return true;
}

inline bool take_u32(const std::string& input, size_t& offset, uint32_t& value) {
  value = 0U;
  for (uint8_t shift = 0U; shift < 32U; shift += 8U) {
    uint8_t part = 0U;
    if (!take_u8(input, offset, part)) return false;
    value |= static_cast<uint32_t>(part) << shift;
  }
  return true;
}

inline bool take_u64(const std::string& input, size_t& offset, uint64_t& value) {
  value = 0U;
  for (uint8_t shift = 0U; shift < 64U; shift += 8U) {
    uint8_t part = 0U;
    if (!take_u8(input, offset, part)) return false;
    value |= static_cast<uint64_t>(part) << shift;
  }
  return true;
}

inline bool take_i32(const std::string& input, size_t& offset, int32_t& value) {
  uint32_t raw = 0U;
  if (!take_u32(input, offset, raw)) return false;
  value = static_cast<int32_t>(raw);
  return true;
}

inline bool append_string(std::string& output, const std::string& value, size_t maximum) {
  if (value.size() > maximum || value.size() > 0xffffU) return false;
  append_u16(output, static_cast<uint16_t>(value.size()));
  output.append(value);
  return true;
}

inline bool take_string(
    const std::string& input,
    size_t& offset,
    std::string& value,
    size_t maximum
) {
  uint16_t length = 0U;
  if (!take_u16(input, offset, length) || length > maximum
      || length > input.size() - offset) {
    return false;
  }
  value.assign(input.data() + offset, length);
  offset += length;
  return true;
}

inline uint32_t crc32(const std::string& value) {
  uint32_t crc = 0xffffffffU;
  for (unsigned char byte : value) {
    crc ^= static_cast<uint32_t>(byte);
    for (uint8_t bit = 0U; bit < 8U; ++bit) {
      crc = (crc & 1U) != 0U ? (crc >> 1U) ^ 0xedb88320U : crc >> 1U;
    }
  }
  return ~crc;
}

inline bool valid_slot(const ScheduleSlot& slot) {
  return slot.hour < 24U && slot.minute < 60U;
}

inline bool validate_payload(const ConfigPayload& payload, std::string& error) {
  if (payload.wifi_ssid.size() > kMaxWifiSsidBytes
      || payload.wifi_pass.size() > kMaxWifiPasswordBytes
      || payload.backend_hostport.size() > kMaxBackendHostportBytes
      || payload.ca_pem.size() > kMaxCaPemBytes
      || payload.device_token.size() > kMaxDeviceTokenBytes
      || payload.device_secret.size() > kMaxDeviceSecretBytes
      || payload.device_id.size() > kMaxDeviceIdBytes
      || payload.auth_state.size() > kMaxAuthStateBytes
      || payload.pairing_id.size() > kMaxPairingIdBytes
      || payload.pairing_nonce.size() > kMaxPairingNonceBytes
      || payload.delivery_mode.size() > kMaxDeliveryModeBytes
      || payload.button_wake_action.size() > kMaxButtonWakeActionBytes) {
    set_error(error, "PAIRING-NVS-001");
    return false;
  }
  if (payload.tz_offset_minutes < -12 * 60 || payload.tz_offset_minutes > 14 * 60
      || payload.refresh_hour >= 24U || payload.refresh_minute >= 60U
      || payload.schedule_count == 0U || payload.schedule_count > kMaxConfigSlots
      || payload.prefetch_lead_minutes > 120U
      || (payload.delivery_mode != "legacy_online"
          && payload.delivery_mode != "stock_compat"
          && payload.delivery_mode != "inktime_offline_schedule")
      || (payload.button_wake_action != "check_new"
          && payload.button_wake_action != "local_next")
      || (payload.auth_state != "unpaired"
          && payload.auth_state != "pairing_pending"
          && payload.auth_state != "paired"
          && payload.auth_state != "credential_issued"
          && payload.auth_state != "revoked"
          && payload.auth_state != "auth_invalid"
          && payload.auth_state != "pairing_expired")) {
    set_error(error, "PAIRING-NVS-001");
    return false;
  }
  if (payload.pairing_retry_attempt > 8U
      || (!payload.pairing_id.empty() && payload.pairing_nonce.empty())
      || (!payload.pairing_nonce.empty() && payload.pairing_id.empty()
          && payload.auth_state != "pairing_pending")
      || (payload.auth_state == "credential_issued"
          && (payload.device_secret.empty() || payload.pairing_id.empty()
              || payload.pairing_nonce.empty()))) {
    set_error(error, "PAIRING-NVS-001");
    return false;
  }
  for (uint8_t index = 0U; index < payload.schedule_count; ++index) {
    if (!valid_slot(payload.schedule_slots[index])) {
      set_error(error, "PAIRING-NVS-001");
      return false;
    }
    if (index > 0U) {
      const uint16_t previous = static_cast<uint16_t>(payload.schedule_slots[index - 1U].hour) * 60U
          + payload.schedule_slots[index - 1U].minute;
      const uint16_t current = static_cast<uint16_t>(payload.schedule_slots[index].hour) * 60U
          + payload.schedule_slots[index].minute;
      if (current <= previous) {
        set_error(error, "PAIRING-NVS-001");
        return false;
      }
    }
  }
  return true;
}

inline bool serialize_payload(const ConfigPayload& payload, std::string& output, std::string& error) {
  if (!validate_payload(payload, error)) return false;
  output.clear();
  output.reserve(512U + payload.ca_pem.size());
  append_u8(output, kPayloadSchemaVersion);
  if (!append_string(output, payload.wifi_ssid, kMaxWifiSsidBytes)
      || !append_string(output, payload.wifi_pass, kMaxWifiPasswordBytes)
      || !append_string(output, payload.backend_hostport, kMaxBackendHostportBytes)
      || !append_string(output, payload.ca_pem, kMaxCaPemBytes)
      || !append_string(output, payload.device_token, kMaxDeviceTokenBytes)) {
    set_error(error, "PAIRING-NVS-001");
    return false;
  }
  append_i32(output, payload.tz_offset_minutes);
  append_u8(output, payload.refresh_hour);
  append_u8(output, payload.refresh_minute);
  append_u8(output, payload.rotate180 ? 1U : 0U);
  append_u8(output, payload.schedule_count);
  append_u16(output, payload.prefetch_lead_minutes);
  if (!append_string(output, payload.delivery_mode, kMaxDeliveryModeBytes)
      || !append_string(output, payload.button_wake_action, kMaxButtonWakeActionBytes)
      || !append_string(output, payload.device_secret, kMaxDeviceSecretBytes)
      || !append_string(output, payload.device_id, kMaxDeviceIdBytes)
      || !append_string(output, payload.auth_state, kMaxAuthStateBytes)
      || !append_string(output, payload.pairing_id, kMaxPairingIdBytes)
      || !append_string(output, payload.pairing_nonce, kMaxPairingNonceBytes)) {
    set_error(error, "PAIRING-NVS-001");
    return false;
  }
  append_u32(output, payload.config_version);
  append_u32(output, payload.credential_version);
  append_u64(output, payload.pairing_expires_at_epoch);
  append_u64(output, payload.pairing_retry_at_epoch);
  append_u8(output, payload.pairing_retry_attempt);
  for (uint8_t index = 0U; index < kMaxConfigSlots; ++index) {
    append_u8(output, payload.schedule_slots[index].hour);
    append_u8(output, payload.schedule_slots[index].minute);
  }
  if (output.size() > kMaxConfigPayloadBytes) {
    set_error(error, "PAIRING-NVS-001");
    return false;
  }
  return true;
}

inline bool deserialize_payload(const std::string& input, ConfigPayload& payload, std::string& error) {
  size_t offset = 0U;
  uint8_t schema = 0U;
  if (!take_u8(input, offset, schema) || (schema != 1U && schema != 2U && schema != kPayloadSchemaVersion)
      || !take_string(input, offset, payload.wifi_ssid, kMaxWifiSsidBytes)
      || !take_string(input, offset, payload.wifi_pass, kMaxWifiPasswordBytes)
      || !take_string(input, offset, payload.backend_hostport, kMaxBackendHostportBytes)
      || !take_string(input, offset, payload.ca_pem, kMaxCaPemBytes)
      || !take_string(input, offset, payload.device_token, kMaxDeviceTokenBytes)
      || !take_i32(input, offset, payload.tz_offset_minutes)
      || !take_u8(input, offset, payload.refresh_hour)
      || !take_u8(input, offset, payload.refresh_minute)) {
    set_error(error, "PAIRING-NVS-004");
    return false;
  }
  uint8_t rotate = 0U;
  if (!take_u8(input, offset, rotate)
      || (rotate != 0U && rotate != 1U)
      || !take_u8(input, offset, payload.schedule_count)
      || !take_u16(input, offset, payload.prefetch_lead_minutes)
      || !take_string(input, offset, payload.delivery_mode, kMaxDeliveryModeBytes)
      || !take_string(input, offset, payload.button_wake_action, kMaxButtonWakeActionBytes)) {
    set_error(error, "PAIRING-NVS-004");
    return false;
  }
  payload.rotate180 = rotate != 0U;
  payload.device_secret.clear();
  payload.device_id.clear();
  payload.auth_state = payload.device_token.empty() ? "unpaired" : "paired";
  payload.credential_version = 0U;
  payload.pairing_id.clear();
  payload.pairing_nonce.clear();
  payload.pairing_expires_at_epoch = 0U;
  payload.pairing_retry_at_epoch = 0U;
  payload.pairing_retry_attempt = 0U;
  if (schema >= 2U
      && (!take_string(input, offset, payload.device_secret, kMaxDeviceSecretBytes)
          || !take_string(input, offset, payload.device_id, kMaxDeviceIdBytes)
          || !take_string(input, offset, payload.auth_state, kMaxAuthStateBytes))) {
    set_error(error, "PAIRING-NVS-004");
    return false;
  }
  if (schema >= 3U
      && (!take_string(input, offset, payload.pairing_id, kMaxPairingIdBytes)
          || !take_string(input, offset, payload.pairing_nonce, kMaxPairingNonceBytes))) {
    set_error(error, "PAIRING-NVS-004");
    return false;
  }
  if (!take_u32(input, offset, payload.config_version)
      || (schema >= 2U && !take_u32(input, offset, payload.credential_version))) {
    set_error(error, "PAIRING-NVS-004");
    return false;
  }
  if (schema >= 3U
      && (!take_u64(input, offset, payload.pairing_expires_at_epoch)
          || !take_u64(input, offset, payload.pairing_retry_at_epoch)
          || !take_u8(input, offset, payload.pairing_retry_attempt))) {
    set_error(error, "PAIRING-NVS-004");
    return false;
  }
  for (uint8_t index = 0U; index < kMaxConfigSlots; ++index) {
    if (!take_u8(input, offset, payload.schedule_slots[index].hour)
        || !take_u8(input, offset, payload.schedule_slots[index].minute)) {
      set_error(error, "PAIRING-NVS-004");
      return false;
    }
  }
  if (offset != input.size() || !validate_payload(payload, error)) {
    if (offset == input.size() && error.empty()) set_error(error, "PAIRING-NVS-004");
    return false;
  }
  return true;
}

inline bool encode_slot(
    const ConfigPayload& payload,
    uint64_t generation,
    std::string& output,
    std::string& error
) {
  std::string serialized;
  if (!serialize_payload(payload, serialized, error)) return false;
  output.clear();
  append_u32(output, kConfigSlotMagic);
  append_u8(output, kEnvelopeVersion);
  append_u8(output, kPayloadSchemaVersion);
  append_u64(output, generation);
  append_u32(output, static_cast<uint32_t>(serialized.size()));
  append_u32(output, crc32(serialized));
  output.append(serialized);
  return output.size() <= kMaxConfigPayloadBytes + 32U;
}

inline bool decode_slot(
    const std::string& input,
    ConfigPayload& payload,
    uint64_t& generation,
    std::string& error
) {
  size_t offset = 0U;
  uint32_t magic = 0U;
  uint8_t envelope = 0U;
  uint8_t schema = 0U;
  uint32_t length = 0U;
  uint32_t expected_crc = 0U;
  if (!take_u32(input, offset, magic) || !take_u8(input, offset, envelope)
      || !take_u8(input, offset, schema) || !take_u64(input, offset, generation)
      || !take_u32(input, offset, length) || !take_u32(input, offset, expected_crc)
      || magic != kConfigSlotMagic || envelope != kEnvelopeVersion
      || (schema != 1U && schema != 2U && schema != kPayloadSchemaVersion) || length > kMaxConfigPayloadBytes
      || length != input.size() - offset) {
    set_error(error, "PAIRING-NVS-004");
    return false;
  }
  const std::string serialized = input.substr(offset, length);
  if (crc32(serialized) != expected_crc || !deserialize_payload(serialized, payload, error)) {
    set_error(error, "PAIRING-NVS-004");
    return false;
  }
  return true;
}

inline bool valid_slot_name(char slot) {
  return slot == 'A' || slot == 'B';
}

inline bool encode_pointer(char active_slot, uint64_t generation, std::string& output, std::string& error) {
  if (!valid_slot_name(active_slot)) {
    set_error(error, "PAIRING-NVS-005");
    return false;
  }
  output.clear();
  append_u32(output, kConfigPointerMagic);
  append_u8(output, kPointerVersion);
  append_u8(output, static_cast<uint8_t>(active_slot));
  append_u64(output, generation);
  append_u32(output, crc32(output));
  return true;
}

inline bool decode_pointer(
    const std::string& input,
    char& active_slot,
    uint64_t& generation,
    std::string& error
) {
  if (input.size() != 18U) {
    set_error(error, "PAIRING-NVS-005");
    return false;
  }
  const uint32_t expected_crc = crc32(input.substr(0U, 14U));
  size_t offset = 0U;
  uint32_t magic = 0U;
  uint8_t version = 0U;
  uint8_t raw_slot = 0U;
  uint32_t actual_crc = 0U;
  if (!take_u32(input, offset, magic) || !take_u8(input, offset, version)
      || !take_u8(input, offset, raw_slot) || !take_u64(input, offset, generation)
      || !take_u32(input, offset, actual_crc)
      || magic != kConfigPointerMagic || version != kPointerVersion
      || !valid_slot_name(static_cast<char>(raw_slot)) || actual_crc != expected_crc) {
    set_error(error, "PAIRING-NVS-005");
    return false;
  }
  active_slot = static_cast<char>(raw_slot);
  return true;
}

inline bool encode_journal(const RecoveryJournal& journal, std::string& output, std::string& error) {
  if (static_cast<uint8_t>(journal.phase) < static_cast<uint8_t>(JournalPhase::Prepared)
      || static_cast<uint8_t>(journal.phase) > static_cast<uint8_t>(JournalPhase::Aborted)) {
    set_error(error, "PAIRING-NVS-005");
    return false;
  }
  if (journal.target_schedule_id.empty() || journal.target_schedule_id.size() > kMaxScheduleIdBytes
      || !valid_slot_name(journal.prepared_slot)
      || (journal.previous_active_slot != 0 && !valid_slot_name(journal.previous_active_slot))) {
    set_error(error, "PAIRING-NVS-005");
    return false;
  }
  output.clear();
  append_u32(output, kScheduleJournalMagic);
  append_u8(output, kJournalVersion);
  append_u8(output, static_cast<uint8_t>(journal.phase));
  append_u8(output, journal.previous_active_slot == 0 ? 0U : static_cast<uint8_t>(journal.previous_active_slot));
  append_u8(output, static_cast<uint8_t>(journal.prepared_slot));
  append_u64(output, journal.previous_generation);
  append_u64(output, journal.prepared_generation);
  append_string(output, journal.target_schedule_id, kMaxScheduleIdBytes);
  append_u32(output, crc32(output));
  return true;
}

inline bool decode_journal(const std::string& input, RecoveryJournal& journal, std::string& error) {
  if (input.size() < 4U + 1U + 1U + 1U + 1U + 8U + 8U + 2U + 4U) {
    set_error(error, "PAIRING-NVS-005");
    return false;
  }
  const uint32_t expected_crc = crc32(input.substr(0U, input.size() - 4U));
  size_t offset = 0U;
  uint32_t magic = 0U;
  uint8_t version = 0U;
  uint8_t phase = 0U;
  uint8_t previous_slot = 0U;
  uint8_t prepared_slot = 0U;
  uint32_t actual_crc = 0U;
  if (!take_u32(input, offset, magic) || !take_u8(input, offset, version)
      || !take_u8(input, offset, phase) || !take_u8(input, offset, previous_slot)
      || !take_u8(input, offset, prepared_slot)
      || !take_u64(input, offset, journal.previous_generation)
      || !take_u64(input, offset, journal.prepared_generation)
      || !take_string(input, offset, journal.target_schedule_id, kMaxScheduleIdBytes)
      || !take_u32(input, offset, actual_crc)
      || offset != input.size() || magic != kScheduleJournalMagic
      || version != kJournalVersion || phase < static_cast<uint8_t>(JournalPhase::Prepared)
      || phase > static_cast<uint8_t>(JournalPhase::Aborted)
      || (previous_slot != 0U && !valid_slot_name(static_cast<char>(previous_slot)))
      || !valid_slot_name(static_cast<char>(prepared_slot)) || actual_crc != expected_crc) {
    set_error(error, "PAIRING-NVS-005");
    return false;
  }
  journal.phase = static_cast<JournalPhase>(phase);
  journal.previous_active_slot = static_cast<char>(previous_slot);
  journal.prepared_slot = static_cast<char>(prepared_slot);
  return true;
}

}  // namespace configstore
}  // namespace inktime
