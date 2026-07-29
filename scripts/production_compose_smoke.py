#!/usr/bin/env python3
from __future__ import annotations

from http.cookiejar import CookieJar
import json
import os
import re
import time
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


BASE_URL = os.environ.get("INKTIME_SMOKE_URL", "http://127.0.0.1:8765").rstrip("/")
USERNAME = "compose-admin"
PASSWORD = "compose-smoke-passphrase"  # noqa: S105 - isolated disposable CI account.
CSRF = re.compile(r'<meta name="csrf-token" content="([^"]+)"')


def main() -> int:
    cookies = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookies))

    def request(path: str, *, form=None, payload=None, csrf: str | None = None):
        headers = {}
        data = None
        if form is not None:
            data = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        if csrf:
            headers["X-CSRF-Token"] = csrf
        return opener.open(
            Request(  # noqa: S310 - BASE_URL is supplied by the trusted CI job.
                BASE_URL + path, data=data, headers=headers
            ),
            timeout=15,
        )

    def page_csrf(path: str) -> str:
        with request(path) as response:
            html = response.read().decode()
        match = CSRF.search(html)
        if match is None:
            raise RuntimeError(f"missing CSRF token at {path}")
        return match.group(1)

    setup_csrf = page_csrf("/setup")
    with request(
        "/setup",
        form={
            "username": USERNAME,
            "password": PASSWORD,
            "password_confirmation": PASSWORD,
            "csrf_token": setup_csrf,
        },
    ) as response:
        if response.status != 200 or not response.url.endswith("/dashboard"):
            raise RuntimeError("setup did not reach dashboard")
    session_cookies = [cookie for cookie in cookies if cookie.name == "session"]
    if len(session_cookies) != 1 or session_cookies[0].secure:
        raise RuntimeError("HTTP smoke session cookie policy is incorrect")

    logout_csrf = page_csrf("/dashboard")
    with request("/logout", form={"csrf_token": logout_csrf}) as response:
        if response.status != 200 or not response.url.endswith("/login"):
            raise RuntimeError("logout did not reach login")

    login_csrf = page_csrf("/login")
    with request(
        "/login",
        form={
            "username": USERNAME,
            "password": PASSWORD,
            "csrf_token": login_csrf,
        },
    ) as response:
        if response.status != 200 or not response.url.endswith("/dashboard"):
            raise RuntimeError("login did not reach dashboard")

    api_csrf = page_csrf("/maintenance")
    with request(
        "/api/v1/maintenance/scan",
        payload={
            "root_path": "/photos",
            "name": "Production Compose Smoke",
            "library_name": "CI Photos",
            "mode": "incremental",
            "build_thumbnails": False,
        },
        csrf=api_csrf,
    ) as response:
        if response.status != 202:
            raise RuntimeError("background scan was not accepted")
        job_id = str(json.load(response)["id"])

    deadline = time.monotonic() + 120
    job_status = ""
    while time.monotonic() < deadline:
        with request(f"/api/v1/jobs/{job_id}") as response:
            job_status = str(json.load(response)["status"])
        if job_status == "completed":
            break
        if job_status in {
            "completed_with_errors",
            "failed",
            "cancelled",
            "budget_exceeded",
        }:
            raise RuntimeError(f"background scan ended in {job_status}")
        time.sleep(1)
    if job_status != "completed":
        raise RuntimeError("worker did not complete background scan before timeout")

    scheduler_seen = False
    while time.monotonic() < deadline:
        with request("/health/detail") as response:
            detail = json.load(response)
        if detail.get("service_heartbeats", {}).get("scheduler"):
            scheduler_seen = True
            break
        time.sleep(1)
    if not scheduler_seen:
        raise RuntimeError("scheduler heartbeat was not observed")
    print("production compose smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
