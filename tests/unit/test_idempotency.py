from __future__ import annotations

import pytest

from inktime.app.core.idempotency import grouped_idempotency_key, scoped_idempotency_key


@pytest.mark.parametrize("length", [1, 127, 128, 129])
def test_grouped_idempotency_key_is_bounded_at_client_key_boundaries(length):
    derived = grouped_idempotency_key("k" * length, "group")

    assert derived is not None
    assert len(derived) <= 128


def test_grouped_idempotency_key_keeps_existing_short_unambiguous_identity():
    assert grouped_idempotency_key("client-key", "2026") == "client-key:2026"


def test_grouped_idempotency_key_normalizes_client_key_once_and_replays():
    assert grouped_idempotency_key("  client-key  ", "年") == grouped_idempotency_key("client-key", "年")
    assert grouped_idempotency_key("k" * 129, "group") == grouped_idempotency_key("k" * 128, "group")
    assert grouped_idempotency_key("client-key", "年") == grouped_idempotency_key("client-key", "年")


def test_grouped_idempotency_key_separates_groups_at_exact_client_key_limit():
    key = "A" * 128

    first = grouped_idempotency_key(key, "group-a")
    second = grouped_idempotency_key(key, "group-b")

    assert first != second
    assert first is not None and len(first) <= 128
    assert second is not None and len(second) <= 128


def test_grouped_idempotency_key_hashes_delimiters_long_groups_and_unicode_exactly():
    assert grouped_idempotency_key("a:b", "c") != grouped_idempotency_key("a", "b:c")
    assert grouped_idempotency_key("client", "group ") != grouped_idempotency_key("client", "group")
    unicode_group = grouped_idempotency_key("用戶🔑", "資料夾/非常長" * 100)
    assert unicode_group != grouped_idempotency_key(
        "用戶🔑", "資料夾/另一個" * 100
    )
    assert unicode_group is not None and len(unicode_group) <= 128


def test_grouped_identity_still_uses_existing_actor_and_endpoint_namespace():
    grouped = grouped_idempotency_key("A" * 128, "group")

    assert grouped is not None
    assert scoped_idempotency_key("ai-mode-run", "actor-a", grouped) != scoped_idempotency_key(
        "ai-mode-run", "actor-b", grouped
    )
    assert scoped_idempotency_key("other-endpoint", "actor-a", grouped) != scoped_idempotency_key(
        "ai-mode-run", "actor-a", grouped
    )
