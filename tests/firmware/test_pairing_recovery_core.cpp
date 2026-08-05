#include "pairing_recovery_core.h"

#include <cassert>
#include <cstdint>

namespace {

using inktime::pairing::RetryState;

class FakeRetryStore {
 public:
  RetryState value;
  bool present = false;
  bool fail_save = false;
  bool fail_clear = false;
  uint32_t save_count = 0U;

  bool save(const RetryState& next) {
    ++save_count;
    if (fail_save) return false;
    value = next;
    present = true;
    return true;
  }

  bool clear() {
    if (fail_clear) return false;
    value = RetryState{};
    present = false;
    return true;
  }
};

void test_invalid_origin_and_ca_failures_persist_bounded_retry() {
  const uint8_t failure_kinds[] = {1U, 2U};  // invalid Origin, invalid CA.
  for (const uint8_t failure_kind : failure_kinds) {
    (void)failure_kind;  // 1 = invalid Origin, 2 = invalid CA.
    FakeRetryStore store;
    RetryState state;
    assert(inktime::pairing::persistFailure(store, state, 1700000000U));
    assert(store.present);
    assert(store.value.attempt == 1U);
    assert(store.value.retry_at_epoch == 1700000060U);
    RetryState restarted = store.value;
    assert(!inktime::pairing::retryDue(restarted, 1700000059U));
    assert(inktime::pairing::retryDue(restarted, 1700000060U));
  }
}

void test_persistence_failure_is_reported_without_advancing_unwritten_state() {
  FakeRetryStore store;
  store.fail_save = true;
  RetryState state;
  assert(!inktime::pairing::persistFailure(store, state, 1700000000U));
  assert(state == RetryState{});
  assert(store.save_count == 1U);
}

void test_clear_failure_is_reported_without_clearing_state() {
  FakeRetryStore store;
  store.fail_clear = true;
  RetryState state = {1700000060U, 1U};
  assert(!inktime::pairing::clearRetryState(store, state));
  assert(state.retry_at_epoch == 1700000060U);
  assert(state.attempt == 1U);
}

void test_cold_restart_without_epoch_uses_bounded_fallback() {
  const RetryState persisted = {1700000060U, 1U};
  assert(inktime::pairing::retryDue(persisted, 0U));
  assert(inktime::pairing::sleepSeconds(persisted, 0U) == 5U * 60U);
}

void test_deadline_and_success_clear() {
  FakeRetryStore store;
  RetryState state = {1700000060U, 1U};
  assert(!inktime::pairing::retryDue(state, 1700000059U));
  assert(inktime::pairing::retryDue(state, 1700000060U));
  assert(inktime::pairing::clearRetryState(store, state));
  assert(!store.present);
  assert(state == RetryState{});
}

void test_retry_cap_does_not_rewrite_unchanged_no_clock_state() {
  FakeRetryStore store;
  RetryState state = {0U, inktime::pairing::kMaximumRetryAttempt};
  assert(inktime::pairing::persistFailure(store, state, 0U));
  assert(store.save_count == 0U);
  assert(state.attempt == inktime::pairing::kMaximumRetryAttempt);
}

}  // namespace

int main() {
  test_invalid_origin_and_ca_failures_persist_bounded_retry();
  test_persistence_failure_is_reported_without_advancing_unwritten_state();
  test_clear_failure_is_reported_without_clearing_state();
  test_cold_restart_without_epoch_uses_bounded_fallback();
  test_deadline_and_success_clear();
  test_retry_cap_does_not_rewrite_unchanged_no_clock_state();
  return 0;
}
