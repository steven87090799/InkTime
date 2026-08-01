#!/usr/bin/env python3
from __future__ import annotations

from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import re
import ssl
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    HTTPCookieProcessor,
    Request,
    build_opener,
)


HTTPS_URL = os.environ.get("INKTIME_TLS_SMOKE_URL", "https://inktime-ci.acme.dev:8443").rstrip("/")
HTTP_URL = os.environ.get("INKTIME_TLS_SMOKE_HTTP_URL", "http://inktime-ci.acme.dev:8080").rstrip("/")
CA_FILE = Path(os.environ["INKTIME_TLS_SMOKE_CA"])
USERNAME = "tls-smoke-admin"
PASSWORD = "tls-smoke-passphrase"  # noqa: S105 - disposable CI-only account
CSRF = re.compile(r'<meta name="csrf-token" content="([^"]+)"')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    plain = build_opener(_NoRedirect())
    try:
        plain.open(Request(HTTP_URL + "/login"), timeout=10)  # noqa: S310 - fixed CI URL
    except HTTPError as exc:
        if exc.code != 301 or exc.headers.get("Location") != HTTPS_URL + "/login":
            raise RuntimeError("HTTP endpoint did not redirect to the expected HTTPS origin") from exc
        if exc.headers.get("Strict-Transport-Security"):
            raise RuntimeError("HTTP redirect must not emit HSTS") from exc
    else:
        raise RuntimeError("HTTP endpoint unexpectedly returned success")

    context = ssl.create_default_context(cafile=str(CA_FILE))
    cookies = CookieJar()
    opener = build_opener(HTTPSHandler(context=context), HTTPCookieProcessor(cookies))

    def request(path: str, *, form=None, csrf_token: str | None = None):
        headers = {}
        data = None
        if form is not None:
            data = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if csrf_token:
            headers["X-CSRF-Token"] = csrf_token
        return opener.open(
            Request(  # noqa: S310 - fixed CI URL with explicit trusted CA
                HTTPS_URL + path, data=data, headers=headers
            ),
            timeout=15,
        )

    def page(path: str) -> tuple[str, str]:
        with request(path) as response:
            html = response.read().decode()
            hsts = response.headers.get("Strict-Transport-Security", "")
        match = CSRF.search(html)
        if match is None:
            raise RuntimeError(f"missing CSRF token at {path}")
        return match.group(1), hsts

    setup_csrf, hsts = page("/setup")
    if hsts != "max-age=31536000; includeSubDomains":
        raise RuntimeError("HTTPS response did not emit the production HSTS policy")
    with request(
        "/setup",
        form={
            "username": USERNAME,
            "password": PASSWORD,
            "password_confirmation": PASSWORD,
            "csrf_token": setup_csrf,
        },
    ) as response:
        if not response.url.endswith("/dashboard"):
            raise RuntimeError("TLS setup did not reach dashboard")

    session_cookies = [cookie for cookie in cookies if cookie.name == "session"]
    if len(session_cookies) != 1:
        raise RuntimeError("expected one Session cookie")
    session_cookie = session_cookies[0]
    rest = {key.casefold(): value for key, value in getattr(session_cookie, "_rest", {}).items()}
    if not session_cookie.secure or "httponly" not in rest or rest.get("samesite") != "Strict":
        raise RuntimeError("Session cookie is missing Secure, HttpOnly or SameSite=Strict")

    dashboard_csrf, _hsts = page("/dashboard")
    with request("/logout", form={"csrf_token": dashboard_csrf}) as response:
        if not response.url.endswith("/login"):
            raise RuntimeError("TLS logout did not reach login")
    login_csrf, _hsts = page("/login")
    with request(
        "/login",
        form={"username": USERNAME, "password": PASSWORD, "csrf_token": login_csrf},
    ) as response:
        if not response.url.endswith("/dashboard"):
            raise RuntimeError("TLS login did not reach dashboard")

    with request("/health/detail") as response:
        health = json.load(response)
    if health.get("production_preflight", {}).get("status") != "ok":
        raise RuntimeError("production preflight is degraded behind TLS proxy")
    runtime = health.get("runtime_config", {})
    if runtime.get("proxy_trust") != 1 or runtime.get("public_url_scheme") != "https":
        raise RuntimeError("proxy hop or public HTTPS diagnostics are incorrect")

    print("production TLS smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
