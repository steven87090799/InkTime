"""Bound TLS handshakes without shortening normal request processing."""
from __future__ import annotations

from collections.abc import Callable
import ssl


def bounded_ssl_context(default_factory: Callable[[], ssl.SSLContext]) -> ssl.SSLContext:
    context = default_factory()
    original_wrap = context.wrap_socket

    def wrap_socket(sock, **kwargs):
        previous_timeout = sock.gettimeout()
        sock.settimeout(5.0)
        # Finish the handshake here so only that phase has a deadline. Gunicorn
        # may call do_handshake again; a completed handshake is a no-op.
        kwargs["do_handshake_on_connect"] = True
        try:
            wrapped = original_wrap(sock, **kwargs)
        except BaseException:
            sock.close()
            raise
        wrapped.settimeout(previous_timeout)
        return wrapped

    context.wrap_socket = wrap_socket  # type: ignore[method-assign, assignment]
    return context
