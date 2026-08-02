#pragma once

#include <Arduino.h>

#include "queue_client_core.h"

struct StoredDisplayRecord {
  String sha256;
  String releaseId;
  String renderProfile;
  String boardProfile;
  int16_t rotation;
  bool succeeded;
  bool valid;
};

struct PendingQueueAck {
  String queueItemId;
  int32_t queueVersion;
  inktime::QueueEvent event;
  bool displaySkipped;
  String errorCode;
  bool delayedTerminal;
  String releaseId;
  bool valid;
};

enum class QueueDownloadResult : uint8_t {
  Used,
  EmptyOrUnsupported,
  Failed,
};
