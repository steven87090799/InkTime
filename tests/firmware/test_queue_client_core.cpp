#include <cassert>
#include <cstring>

#include "queue_client_core.h"

using namespace inktime;

int main() {
  const char* sha = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
  const char* changedSha = "1123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
  const char* path = "/api/device/v1/queue/items/item-1/files/photo.bin";
  QueueItemContract valid = {"item-1", "release-1", sha, path, true, 96000};
  assert(validQueueItem(valid));
  valid.sizeIsInteger = false;
  assert(!validQueueItem(valid));
  valid.sizeIsInteger = true;
  valid.downloadUrl = "https://evil.example/photo.bin";
  assert(!validQueueItem(valid));
  valid.downloadUrl = "//evil.example/photo.bin";
  assert(!validQueueItem(valid));
  valid.downloadUrl = "/api/device/v1/queue/items/../secret/files/photo.bin";
  assert(!validQueueItem(valid));
  valid.downloadUrl = "/api/device/v1/queue/items/item\\evil/files/photo.bin";
  assert(!validQueueItem(valid));
  valid.downloadUrl = "/api/device/v1/queue/items/another-item/files/photo.bin";
  assert(!validQueueItem(valid));
  const char embeddedNul[] = "/api/device/v1/queue/items/item\0/files/photo.bin";
  assert(!isSafeQueueDownloadPath(embeddedNul, sizeof(embeddedNul) - 1U));
  assert(queueManifestDecision(404, false, 0, false, 0) == QueueManifestDecision::FallbackLatest);
  assert(queueManifestDecision(409, false, 0, false, 0) == QueueManifestDecision::FallbackLatest);
  assert(queueManifestDecision(200, true, 100, true, 0) == QueueManifestDecision::FallbackLatest);
  assert(queueManifestDecision(200, true, 100, true, 1) == QueueManifestDecision::UseQueue);
  assert(queueManifestDecision(200, true, 100, false, 1) == QueueManifestDecision::Reject);
  assert(queueManifestDecision(200, false, 100, true, 1) == QueueManifestDecision::Reject);
  assert(queueManifestDecision(200, true, kQueueManifestMaxBytes + 1, true, 1)
         == QueueManifestDecision::Reject);

  assert(queueEventCanFollow(QueueEvent::ManifestReceived, QueueEvent::DownloadStarted));
  assert(queueEventCanFollow(QueueEvent::HashVerified, QueueEvent::DisplayCompleted));
  assert(!queueEventCanFollow(QueueEvent::DownloadStarted, QueueEvent::HashVerified));
  assert(ackDecision(200, 0) == AckDecision::Accepted);
  assert(ackDecision(409, 0) == AckDecision::StaleManifest);
  assert(ackDecision(401, 0) == AckDecision::AuthorizationFailed);
  assert(ackDecision(503, 0) == AckDecision::Retry);
  assert(ackDecision(503, kQueueRetryLimit) == AckDecision::Stop);
  assert(ackDecision(429, 0) == AckDecision::Retry);
  assert(ackDecision(429, kQueueRetryLimit) == AckDecision::Stop);

  char first[256] = {};
  char restarted[256] = {};
  char otherEvent[256] = {};
  assert(idempotencyMaterial("item-1", 7, QueueEvent::HashVerified, first, sizeof(first)));
  assert(idempotencyMaterial("item-1", 7, QueueEvent::HashVerified, restarted, sizeof(restarted)));
  assert(strcmp(first, restarted) == 0);
  assert(idempotencyMaterial("item-1", 7, QueueEvent::DisplayStarted, otherEvent, sizeof(otherEvent)));
  assert(strcmp(first, otherEvent) != 0);

  DisplayRecord record = {sha, "release-1", "safe_4c", "gdey073d46", 0, true, true};
  DisplayCandidate candidate = {
    sha, "release-2", "safe_4c", "gdey073d46", 0, true, true, false, false, false,
  };
  assert(shouldSkipDisplay(record, candidate));
  candidate.rotation = 180;
  assert(!shouldSkipDisplay(record, candidate));
  candidate.rotation = 0;
  candidate.sha256 = changedSha;
  candidate.releaseId = "same-release";
  assert(!shouldSkipDisplay(record, candidate));
  candidate.sha256 = sha;
  record.releaseId = "same-release";
  record.displaySucceeded = false;
  assert(!shouldSkipDisplay(record, candidate));
  record.displaySucceeded = true;
  candidate.cacheVerified = false;
  assert(!shouldSkipDisplay(record, candidate));
  candidate.cacheVerified = true;
  candidate.forcedRefresh = true;
  assert(!shouldSkipDisplay(record, candidate));
  candidate.forcedRefresh = false;
  record.structurallyValid = false;
  assert(!shouldSkipDisplay(record, candidate));
  return 0;
}
