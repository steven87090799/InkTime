from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from inktime.app.core.server_tls import bounded_ssl_context


def test_tls_deadline_restores_original_timeout_and_preserves_security_options():
    raw = Mock()
    raw.gettimeout.return_value = None
    wrapped = Mock()
    original_wrap = Mock(return_value=wrapped)
    context = SimpleNamespace(wrap_socket=original_wrap, verify_mode="unchanged")
    result = bounded_ssl_context(lambda: context)
    assert result is context
    assert result.wrap_socket(raw, server_side=True, do_handshake_on_connect=False) is wrapped
    raw.settimeout.assert_called_once_with(5.0)
    original_wrap.assert_called_once_with(raw, server_side=True, do_handshake_on_connect=True)
    wrapped.settimeout.assert_called_once_with(None)
    assert context.verify_mode == "unchanged"
    raw.close.assert_not_called()


def test_stalled_tls_socket_is_closed_and_timeout_reaches_worker():
    raw = Mock()
    original_wrap = Mock(side_effect=TimeoutError("TLS handshake timed out"))
    context = SimpleNamespace(wrap_socket=original_wrap)
    bounded_ssl_context(lambda: context)
    with pytest.raises(TimeoutError, match="TLS handshake timed out"):
        context.wrap_socket(raw, server_side=True)
    raw.close.assert_called_once_with()
