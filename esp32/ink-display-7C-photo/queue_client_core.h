#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

namespace inktime {

constexpr size_t kQueueManifestMaxBytes = 32768U;
constexpr size_t kQueueIdentifierMaxBytes = 128U;
constexpr size_t kQueueDownloadPathMaxBytes = 512U;
constexpr uint8_t kQueueRetryLimit = 2U;

enum class QueueEvent : uint8_t {
  ManifestReceived,
  DownloadStarted,
  DownloadCompleted,
  HashVerified,
  DisplayStarted,
  DisplayCompleted,
  DisplayFailed,
};

enum class AckDecision : uint8_t {
  Accepted,
  Retry,
  StaleManifest,
  AuthorizationFailed,
  Stop,
};

enum class QueueAckResultDisposition : uint8_t {
  RetainPending,
  Accepted,
  Stale,
  AuthoritativePermanentReject,
};

enum class QueueManifestDecision : uint8_t {
  UseQueue,
  FallbackLatest,
  Reject,
};

struct QueueItemContract {
  const char* queueItemId;
  const char* releaseId;
  const char* sha256;
  const char* downloadUrl;
  bool sizeIsInteger;
  int64_t size;
};

struct DisplayRecord {
  const char* sha256;
  const char* releaseId;
  const char* renderProfile;
  const char* boardProfile;
  int16_t rotation;
  bool displaySucceeded;
  bool structurallyValid;
};

struct DisplayCandidate {
  const char* sha256;
  const char* releaseId;
  const char* renderProfile;
  const char* boardProfile;
  int16_t rotation;
  bool payloadVerified;
  bool cacheVerified;
  bool forcedRefresh;
  bool recoveryRequested;
  bool diagnosticRequested;
};

inline const char* queueEventName(QueueEvent event) {
  switch (event) {
    case QueueEvent::ManifestReceived: return "MANIFEST_RECEIVED";
    case QueueEvent::DownloadStarted: return "DOWNLOAD_STARTED";
    case QueueEvent::DownloadCompleted: return "DOWNLOAD_COMPLETED";
    case QueueEvent::HashVerified: return "HASH_VERIFIED";
    case QueueEvent::DisplayStarted: return "DISPLAY_STARTED";
    case QueueEvent::DisplayCompleted: return "DISPLAY_COMPLETED";
    case QueueEvent::DisplayFailed: return "DISPLAY_FAILED";
  }
  return "DISPLAY_FAILED";
}

inline bool boundedText(const char* value, size_t maximum) {
  if (value == nullptr) return false;
  const size_t length = strnlen(value, maximum + 1U);
  return length > 0U && length <= maximum;
}

inline bool isSha256HexValue(const char* value) {
  if (value == nullptr || strnlen(value, 65U) != 64U) return false;
  for (size_t index = 0; index < 64U; ++index) {
    const char character = value[index];
    if (!((character >= '0' && character <= '9')
          || (character >= 'a' && character <= 'f')
          || (character >= 'A' && character <= 'F'))) {
      return false;
    }
  }
  return true;
}

inline bool isSafeQueueDownloadPath(const char* value, size_t length) {
  static constexpr char kPrefix[] = "/api/device/v1/queue/items/";
  if (value == nullptr || length < sizeof(kPrefix) || length > kQueueDownloadPathMaxBytes
      || memcmp(value, kPrefix, sizeof(kPrefix) - 1U) != 0 || value[1] == '/') {
    return false;
  }
  for (size_t index = 0; index < length; ++index) {
    const char character = value[index];
    if (character == '\0' || character == '\\' || character == '?' || character == '#') return false;
    if (character == '.' && index + 1U < length && value[index + 1U] == '.') return false;
  }
  return true;
}

inline bool isSafeQueueDownloadPathForItem(
  const char* value,
  size_t length,
  const char* queueItemId
) {
  static constexpr char kPrefix[] = "/api/device/v1/queue/items/";
  static constexpr char kFileSeparator[] = "/files/";
  if (!isSafeQueueDownloadPath(value, length)
      || !boundedText(queueItemId, kQueueIdentifierMaxBytes)) {
    return false;
  }
  const size_t prefixLength = sizeof(kPrefix) - 1U;
  const size_t itemLength = strlen(queueItemId);
  const size_t separatorLength = sizeof(kFileSeparator) - 1U;
  return length > prefixLength + itemLength + separatorLength
    && memcmp(value + prefixLength, queueItemId, itemLength) == 0
    && memcmp(value + prefixLength + itemLength, kFileSeparator, separatorLength) == 0;
}

inline bool validQueueItem(const QueueItemContract& item) {
  return boundedText(item.queueItemId, kQueueIdentifierMaxBytes)
    && boundedText(item.releaseId, kQueueIdentifierMaxBytes)
    && isSha256HexValue(item.sha256)
    && item.sizeIsInteger && item.size > 0 && item.size <= 64LL * 1024LL * 1024LL
    && item.downloadUrl != nullptr
    && isSafeQueueDownloadPathForItem(
      item.downloadUrl, strlen(item.downloadUrl), item.queueItemId);
}

inline QueueManifestDecision queueManifestDecision(
  int statusCode,
  bool jsonContentType,
  int64_t bodySize,
  bool schemaValid,
  size_t itemCount
) {
  if (statusCode == 404 || statusCode == 409) return QueueManifestDecision::FallbackLatest;
  if (statusCode != 200 || !jsonContentType || bodySize <= 0
      || bodySize > static_cast<int64_t>(kQueueManifestMaxBytes) || !schemaValid) {
    return QueueManifestDecision::Reject;
  }
  return itemCount == 0U ? QueueManifestDecision::FallbackLatest
                         : QueueManifestDecision::UseQueue;
}

inline bool queueEventCanFollow(QueueEvent previous, QueueEvent next) {
  if (next == QueueEvent::DisplayFailed) return previous != QueueEvent::DisplayCompleted;
  switch (previous) {
    case QueueEvent::ManifestReceived: return next == QueueEvent::DownloadStarted;
    case QueueEvent::DownloadStarted: return next == QueueEvent::DownloadCompleted;
    case QueueEvent::DownloadCompleted: return next == QueueEvent::HashVerified;
    case QueueEvent::HashVerified:
      return next == QueueEvent::DisplayStarted || next == QueueEvent::DisplayCompleted;
    case QueueEvent::DisplayStarted: return next == QueueEvent::DisplayCompleted;
    case QueueEvent::DisplayCompleted:
    case QueueEvent::DisplayFailed: return false;
  }
  return false;
}

inline AckDecision ackDecision(int statusCode, uint8_t attempts) {
  if (statusCode >= 200 && statusCode < 300) return AckDecision::Accepted;
  if (statusCode == 409) return AckDecision::StaleManifest;
  if (statusCode == 401 || statusCode == 403) return AckDecision::AuthorizationFailed;
  if ((statusCode <= 0 || statusCode == 429 || statusCode >= 500) && attempts < kQueueRetryLimit) {
    return AckDecision::Retry;
  }
  return AckDecision::Stop;
}

inline QueueAckResultDisposition queueAckResultDisposition(
  const char* expectedQueueItemId,
  const char* expectedEvent,
  const char* responseQueueItemId,
  const char* responseEvent,
  int httpStatus,
  const char* outcome,
  const char* errorCode
) {
  const bool identityMatches = boundedText(expectedQueueItemId, kQueueIdentifierMaxBytes)
    && boundedText(expectedEvent, kQueueIdentifierMaxBytes)
    && boundedText(responseQueueItemId, kQueueIdentifierMaxBytes)
    && boundedText(responseEvent, kQueueIdentifierMaxBytes)
    && strcmp(expectedQueueItemId, responseQueueItemId) == 0
    && strcmp(expectedEvent, responseEvent) == 0;
  if (!identityMatches || !boundedText(outcome, 32U)) return QueueAckResultDisposition::RetainPending;
  if (strcmp(outcome, "accepted") == 0 && httpStatus >= 200 && httpStatus < 300) {
    return QueueAckResultDisposition::Accepted;
  }
  if (strcmp(outcome, "rejected") == 0 && httpStatus == 409
      && boundedText(errorCode, 64U) && strcmp(errorCode, "QUEUE-003") == 0) {
    return QueueAckResultDisposition::Stale;
  }
  if (strcmp(outcome, "rejected") == 0 && httpStatus >= 400 && httpStatus < 500
      && httpStatus != 409 && httpStatus != 429 && boundedText(errorCode, 64U)) {
    return QueueAckResultDisposition::AuthoritativePermanentReject;
  }
  return QueueAckResultDisposition::RetainPending;
}

inline bool queueAckMayUnlockDisplay(
  bool authoritativePermanentReject,
  bool allResolved,
  bool unresolvedOther,
  bool durabilityFailure
) {
  return authoritativePermanentReject && allResolved && !unresolvedOther && !durabilityFailure;
}

inline bool idempotencyMaterial(
  const char* queueItemId,
  int64_t queueVersion,
  QueueEvent event,
  char* output,
  size_t outputSize
) {
  if (!boundedText(queueItemId, kQueueIdentifierMaxBytes) || queueVersion < 0
      || output == nullptr || outputSize < 32U) {
    return false;
  }
  const int written = snprintf(
    output,
    outputSize,
    "inktime-queue-v1|%s|%lld|%s",
    queueItemId,
    static_cast<long long>(queueVersion),
    queueEventName(event)
  );
  return written > 0 && static_cast<size_t>(written) < outputSize;
}

inline bool shouldSkipDisplay(const DisplayRecord& record, const DisplayCandidate& candidate) {
  return record.structurallyValid && record.displaySucceeded
    && isSha256HexValue(record.sha256) && isSha256HexValue(candidate.sha256)
    && strcmp(record.sha256, candidate.sha256) == 0
    && boundedText(record.renderProfile, 64U) && boundedText(candidate.renderProfile, 64U)
    && strcmp(record.renderProfile, candidate.renderProfile) == 0
    && boundedText(record.boardProfile, 96U) && boundedText(candidate.boardProfile, 96U)
    && strcmp(record.boardProfile, candidate.boardProfile) == 0
    && record.rotation == candidate.rotation
    && candidate.payloadVerified && candidate.cacheVerified
    && !candidate.forcedRefresh && !candidate.recoveryRequested && !candidate.diagnosticRequested;
}

}  // namespace inktime
