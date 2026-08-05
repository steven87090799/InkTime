#pragma once

#include <Arduino.h>
#include <Preferences.h>

#include "device_config_store_core.h"

namespace inktime {

class DeviceConfigStore final {
 public:
  struct Prepared {
    configstore::ConfigPayload payload;
    char prepared_slot = 0;
    uint64_t prepared_generation = 0U;
    char previous_active_slot = 0;
    uint64_t previous_generation = 0U;
  };

  explicit DeviceConfigStore(const char* storage_namespace = "cfgstore")
      : storage_namespace_(storage_namespace == nullptr ? "cfgstore" : storage_namespace) {}

  bool load(configstore::ConfigPayload& payload, String& error, String* warning = nullptr);
  bool save(const configstore::ConfigPayload& payload, String& error);
  bool prepare(const configstore::ConfigPayload& payload, Prepared& prepared, String& error);
  bool commit(const Prepared& prepared, String& error);
  bool commitPreparedSlot(
      char prepared_slot,
      uint64_t prepared_generation,
      const configstore::ConfigPayload& payload,
      String& error);
  bool readActive(
      configstore::ConfigPayload& payload,
      char& active_slot,
      uint64_t& generation,
      String& error);
  bool readPrepared(
      char prepared_slot,
      uint64_t prepared_generation,
      configstore::ConfigPayload& payload,
      String& error);
  bool readJournal(configstore::RecoveryJournal& journal, bool& present, String& error);
  bool writeJournal(const configstore::RecoveryJournal& journal, String& error);
  bool clearJournal(String& error);
  bool clearAll(String& error);

 private:
  struct SlotValue {
    configstore::ConfigPayload payload;
    char slot = 0;
    uint64_t generation = 0U;
  };

  bool readBlob(
      Preferences& store,
      const char* key,
      std::string& bytes,
      bool& present,
      String& error) const;
  bool readSlot(Preferences& store, char slot, SlotValue& value, String& error) const;
  bool readPointer(
      Preferences& store,
      char& active_slot,
      uint64_t& generation,
      bool& present,
      String& error) const;
  bool writePointer(
      Preferences& store,
      char active_slot,
      uint64_t generation,
      String& error) const;
  bool clearSlot(char slot, String& error) const;
  bool findNewest(Preferences& store, SlotValue& value, String& error) const;
  bool readCurrent(
      Preferences& store,
      SlotValue& value,
      bool& present,
      String& error) const;
  bool loadLegacy(configstore::ConfigPayload& payload, bool& present, String& error) const;
  bool removeLegacyFormalKeys(String& error) const;
  static bool parseLegacyClock(const String& value, configstore::ScheduleSlot& slot);
  static void setError(String& error, const char* value);

  const char* storage_namespace_;
};

}  // namespace inktime
