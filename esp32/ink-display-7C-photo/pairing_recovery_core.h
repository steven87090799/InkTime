#pragma once

#include <cstdint>

namespace inktime {
namespace pairing {

constexpr uint8_t kMaximumRetryAttempt = 8U;

struct RetryState {
  uint64_t retry_at_epoch = 0U;
  uint8_t attempt = 0U;
};

inline bool operator==(const RetryState& left, const RetryState& right) {
  return left.retry_at_epoch == right.retry_at_epoch && left.attempt == right.attempt;
}

inline uint32_t backoffSeconds(uint8_t attempt) {
  if (attempt == 0U) return 60U;
  if (attempt == 1U) return 5U * 60U;
  if (attempt == 2U) return 15U * 60U;
  return 60U * 60U;
}

inline RetryState nextRetryState(const RetryState& current, uint64_t now_epoch) {
  RetryState next = current;
  const uint8_t previous_attempt = current.attempt;
  next.attempt = previous_attempt < kMaximumRetryAttempt
    ? static_cast<uint8_t>(previous_attempt + 1U) : kMaximumRetryAttempt;
  if (previous_attempt >= kMaximumRetryAttempt) {
    if (now_epoch != 0U && current.retry_at_epoch <= now_epoch) {
      next.retry_at_epoch = now_epoch + backoffSeconds(previous_attempt);
    }
    return next;
  }
  next.retry_at_epoch = now_epoch == 0U
    ? 0U : now_epoch + backoffSeconds(previous_attempt);
  return next;
}

inline bool retryDue(const RetryState& state, uint64_t now_epoch) {
  // Without a trusted clock, allow one bounded recovery probe. The caller
  // uses attempt-based sleep so this cannot become a tight retry loop.
  if (state.retry_at_epoch == 0U || now_epoch == 0U) return true;
  return now_epoch >= state.retry_at_epoch;
}

inline uint32_t sleepSeconds(const RetryState& state, uint64_t now_epoch) {
  if (now_epoch != 0U && state.retry_at_epoch > now_epoch) {
    const uint64_t remaining = state.retry_at_epoch - now_epoch;
    return remaining > 3600U ? 3600U : static_cast<uint32_t>(remaining);
  }
  return backoffSeconds(state.attempt);
}

template <typename Store>
inline bool persistFailure(Store& store, RetryState& state, uint64_t now_epoch) {
  const RetryState next = nextRetryState(state, now_epoch);
  if (next == state) return true;
  if (!store.save(next)) return false;
  state = next;
  return true;
}

template <typename Store>
inline bool clearRetryState(Store& store, RetryState& state) {
  if (!store.clear()) return false;
  state = RetryState{};
  return true;
}

}  // namespace pairing
}  // namespace inktime
