#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from inktime.app.db.migrations import MIGRATIONS

BASE_URL = os.environ.get("INKTIME_LAN_GATE_URL", "http://127.0.0.1:8765").rstrip("/")
USERNAME = "lan-gate-admin"
PASSWORD = "lan-gate-passphrase"  # noqa: S105 - isolated disposable CI account
CSRF = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
RELEASE_ID = "lan-gate-release-v1"
EXPECTED_MIGRATION_VERSION = max(migration.version for migration in MIGRATIONS)


def _read_state(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _state_int(state: dict[str, object], key: str) -> int:
    value = state.get(key)
    if type(value) is not int:
        raise RuntimeError(f"invalid integer state field: {key}")
    return value


def _write_state(path: Path, state: dict[str, object]) -> None:
    path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


class Browser:
    def __init__(self) -> None:
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    def request(
        self,
        path: str,
        *,
        form: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
        csrf: str | None = None,
        bearer: str | None = None,
        credential_version: int | None = None,
    ):
        headers: dict[str, str] = {}
        data = None
        if form is not None:
            data = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        if csrf:
            headers["X-CSRF-Token"] = csrf
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if credential_version is not None:
            headers["X-InkTime-Credential-Version"] = str(credential_version)
        return self.opener.open(
            Request(  # noqa: S310 - fixed trusted CI LAN origin
                BASE_URL + path, data=data, headers=headers
            ),
            timeout=30,
        )

    def csrf(self, path: str) -> str:
        with self.request(path) as response:
            body = response.read().decode()
        match = CSRF.search(body)
        if match is None:
            raise RuntimeError(f"missing CSRF token at {path}")
        return match.group(1)

    def login(self) -> None:
        with self.request(
            "/login",
            form={
                "username": USERNAME,
                "password": PASSWORD,
                "csrf_token": self.csrf("/login"),
            },
        ) as response:
            if not response.url.endswith("/dashboard"):
                raise RuntimeError("LAN login did not reach dashboard")


def _initialize(state_path: Path) -> None:
    browser = Browser()
    with browser.request(
        "/setup",
        form={
            "username": USERNAME,
            "password": PASSWORD,
            "password_confirmation": PASSWORD,
            "csrf_token": browser.csrf("/setup"),
        },
    ) as response:
        if not response.url.endswith("/dashboard"):
            raise RuntimeError("LAN setup did not reach dashboard")
        if response.headers.get("Strict-Transport-Security"):
            raise RuntimeError("LAN HTTP response must not emit HSTS")
    sessions = [cookie for cookie in browser.cookies if cookie.name == "session"]
    if len(sessions) != 1 or sessions[0].secure:
        raise RuntimeError("LAN Session cookie must be non-Secure only in explicit HTTP mode")
    rest = {key.casefold(): value for key, value in getattr(sessions[0], "_rest", {}).items()}
    if "httponly" not in rest or rest.get("samesite") != "Strict":
        raise RuntimeError("LAN Session cookie lost HttpOnly or SameSite=Strict")

    csrf = browser.csrf("/settings")
    with browser.request("/api/v1/settings", payload={"general.timezone": "UTC"}, csrf=csrf) as response:
        if response.status != 200:
            raise RuntimeError("representative setting update failed")
    device_id = "lan-gate-esp32"
    pairing_nonce = "lan-gate-" + secrets.token_urlsafe(24)
    with browser.request(
        "/api/device/v1/pairing/request",
        payload={
            "device_id": device_id,
            "pairing_nonce": pairing_nonce,
            "firmware_identity": "LAN-Gate-ESP32",
            "firmware_version": "2.6.0",
            "panel_profile": "safe_4c",
            "capabilities": {"automatic_pairing": True, "ab_credential_store": True},
        },
    ) as response:
        if response.status != 201:
            raise RuntimeError("custom ESP32 pairing request failed")
        pairing = json.load(response)
    pairing_id = str(pairing["pairing_id"])
    pairing_code = str(pairing["pairing_code"])
    if len(pairing_code) != 6 or not pairing_code.isdigit():
        raise RuntimeError("custom ESP32 pairing response did not contain a physical code")
    with browser.request("/api/v1/device-pairing/pending") as response:
        pending = json.load(response)
    if pairing_code in json.dumps(pending, ensure_ascii=False):
        raise RuntimeError("admin pending pairing response leaked the physical code")
    with browser.request(
        f"/api/v1/device-pairing/{pairing_id}/approve",
        payload={
            "pairing_code": pairing_code,
            "device_config": {
                "name": "LAN Gate Device",
                "timezone": "Asia/Taipei",
                "schedule": "08:00",
                "schedule_times": ["08:00"],
                "panel_profile": "safe_4c",
            },
        },
        csrf=csrf,
    ) as response:
        if response.status != 200:
            raise RuntimeError("custom ESP32 admin approval failed")
    with browser.request(
        "/api/device/v1/pairing/claim",
        payload={"pairing_id": pairing_id, "pairing_nonce": pairing_nonce},
    ) as response:
        if response.status != 200:
            raise RuntimeError("custom ESP32 credential claim failed")
        credential = json.load(response)
    device_secret = str(credential["device_secret"])
    credential_version = int(credential["credential_version"])
    _write_state(
        state_path,
        {
            "device_id": device_id,
            "device_secret": device_secret,
            "credential_version": credential_version,
            "pairing_id": pairing_id,
        },
    )
    with browser.request(
        "/api/device/v1/pairing/confirm",
        payload={
            "pairing_id": pairing_id,
            "device_id": device_id,
            "pairing_nonce": pairing_nonce,
        },
        bearer=device_secret,
        credential_version=credential_version,
    ) as response:
        if response.status != 200 or json.load(response).get("status") not in {"confirmed", "already_confirmed"}:
            raise RuntimeError("custom ESP32 credential confirm failed")
    with browser.request(
        "/api/device/v1/status",
        payload={},
        bearer=device_secret,
        credential_version=credential_version,
    ) as response:
        if response.status != 200:
            raise RuntimeError("custom ESP32 authenticated status failed after confirm")
    with browser.request("/health/detail") as response:
        detail = json.load(response)
    preflight = detail.get("production_preflight", {})
    runtime = detail.get("runtime_config", {})
    if (
        runtime.get("environment") != "production"
        or preflight.get("transport") != "trusted-lan-http"
        or preflight.get("security_state") != "degraded"
        or preflight.get("tls_enabled") is not False
        or preflight.get("secure_cookie") is not False
    ):
        raise RuntimeError("LAN production diagnostics are not explicitly degraded")
    maintenance_csrf = browser.csrf("/maintenance")
    with browser.request(
        "/api/v1/maintenance/scan",
        payload={
            "root_path": "/photos",
            "name": "LAN Production Gate",
            "library_name": "LAN Gate Photos",
            "mode": "incremental",
            "build_thumbnails": False,
        },
        csrf=maintenance_csrf,
    ) as response:
        job_id = str(json.load(response)["id"])
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        with browser.request(f"/api/v1/jobs/{job_id}") as response:
            job_status = str(json.load(response)["status"])
        if job_status == "completed":
            break
        if job_status in {"completed_with_errors", "failed", "cancelled", "budget_exceeded"}:
            raise RuntimeError(f"LAN worker job ended in {job_status}")
        time.sleep(1)
    else:
        raise RuntimeError("LAN worker job timed out")
    while time.monotonic() < deadline:
        with browser.request("/health/detail") as response:
            if json.load(response).get("service_heartbeats", {}).get("scheduler"):
                break
        time.sleep(1)
    else:
        raise RuntimeError("LAN scheduler heartbeat was not observed")
    logout_csrf = browser.csrf("/dashboard")
    with browser.request("/logout", form={"csrf_token": logout_csrf}) as response:
        if not response.url.endswith("/login"):
            raise RuntimeError("LAN logout failed")
    browser.login()
    _write_state(
        state_path,
        {
            "device_id": device_id,
            "device_secret": device_secret,
            "credential_version": credential_version,
            "pairing_id": pairing_id,
        },
    )


def _seed_release(state_path: Path, data_dir: Path) -> None:
    state = _read_state(state_path)
    device_id = str(state["device_id"])
    database_path = data_dir / "inktime.db"
    release_dir = data_dir / "releases" / RELEASE_ID
    release_dir.mkdir(parents=True, exist_ok=True)
    payload = bytes([0x55]) * 96_000
    digest = sha256(payload).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": 2,
        "release_id": RELEASE_ID,
        "display_type": "epaper",
        "width": 480,
        "height": 800,
        "pixel_format": "2bpp",
        "render_profile": "safe_4c",
        "created_at": now,
        "files": [{"name": "photo_1.bin", "size": len(payload), "sha256": digest}],
    }
    (release_dir / "photo_1.bin").write_bytes(payload)
    (release_dir / "manifest.json").write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT OR REPLACE INTO releases(
                id,display_type,width,height,pixel_format,manifest_json,status,created_at,
                published_at,created_by,render_profile,failure_reason,verified_at,reconciliation_status
            ) VALUES (?,?,?,?,?,?,'published',?,?,?,?,NULL,?,'ok')
            """,
            (
                RELEASE_ID,
                "epaper",
                480,
                800,
                "2bpp",
                json.dumps(manifest, separators=(",", ":")),
                now,
                now,
                "lan-gate",
                "safe_4c",
                now,
            ),
        )
        connection.execute(
            "INSERT OR REPLACE INTO device_render_releases(device_id,release_id,assigned_at) VALUES (?,?,?)",
            (device_id, RELEASE_ID, now),
        )
        connection.commit()
        migration = int(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])
        if migration != EXPECTED_MIGRATION_VERSION:
            raise RuntimeError(f"unexpected migration version: {migration}")
    finally:
        connection.close()
    state["release_sha256"] = digest
    state["session_key_sha256"] = sha256((data_dir / "session.key").read_bytes()).hexdigest()
    _write_state(state_path, state)


def _exercise(state_path: Path) -> None:
    state = _read_state(state_path)
    device_id = str(state["device_id"])
    secret = str(state["device_secret"])
    credential_version = _state_int(state, "credential_version")
    expected_sha = str(state["release_sha256"])
    browser = Browser()
    browser.login()
    with browser.request("/api/v1/settings/export") as response:
        if json.load(response)["settings"].get("general.timezone") != "UTC":
            raise RuntimeError("representative setting did not persist")
    with browser.request(
        "/api/device/v1/releases/latest", bearer=secret, credential_version=credential_version
    ) as response:
        latest = json.load(response)
    file_path = latest["download_base_url"] + latest["files"][0]["name"]
    with browser.request(file_path, bearer=secret, credential_version=credential_version) as response:
        if sha256(response.read()).hexdigest() != expected_sha:
            raise RuntimeError("Latest Release download hash mismatch")

    csrf = browser.csrf("/device-queues")
    with browser.request(
        f"/api/devices/{device_id}/queue/generate",
        payload={"release_id": RELEASE_ID, "depth": 3},
        csrf=csrf,
    ) as response:
        if response.status != 201:
            raise RuntimeError("Queue generation failed")
    with browser.request(
        "/api/device/v1/queue/manifest", bearer=secret, credential_version=credential_version
    ) as response:
        queue_manifest = json.load(response)
    item = queue_manifest["items"][0]
    with browser.request(
        str(item["download_url"]), bearer=secret, credential_version=credential_version
    ) as response:
        if sha256(response.read()).hexdigest() != expected_sha:
            raise RuntimeError("Queue download hash mismatch")
    for index, event in enumerate(
        ("MANIFEST_RECEIVED", "DOWNLOAD_STARTED", "DOWNLOAD_COMPLETED", "HASH_VERIFIED")
    ):
        with browser.request(
            "/api/device/v1/queue/ack",
            payload={
                "queue_item_id": item["queue_item_id"],
                "queue_version": queue_manifest["queue_version"],
                "event": event,
                "idempotency_key": f"lan-gate-{index}",
            },
            bearer=secret,
            credential_version=credential_version,
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Queue ACK failed: {event}")
    completed = {
        "queue_item_id": item["queue_item_id"],
        "queue_version": queue_manifest["queue_version"],
        "event": "DISPLAY_COMPLETED",
        "idempotency_key": "lan-gate-completed",
        "display_skipped": True,
        "skip_reason": "same_sha256",
    }
    for _attempt in range(2):
        with browser.request(
            "/api/device/v1/queue/ack",
            payload=completed,
            bearer=secret,
            credential_version=credential_version,
        ) as response:
            if response.status != 200:
                raise RuntimeError("idempotent DISPLAY_COMPLETED failed")
    with browser.request(
        "/api/device/v1/status",
        payload={
            "firmware_version": "lan-gate",
            "display_updated": False,
            "display_skipped": True,
            "display_skip_reason": "same_sha256",
            "payload_sha256_verified": True,
            "release_id": RELEASE_ID,
            "render_profile": "safe_4c",
        },
        bearer=secret,
        credential_version=credential_version,
    ) as response:
        if response.status != 200:
            raise RuntimeError("same-content Device Status failed")
    with browser.request("/api/v1/backups", payload={}, csrf=csrf) as response:
        backup = json.load(response)
    state["backup_name"] = backup["name"]
    _write_state(state_path, state)


def _verify(state_path: Path) -> None:
    state = _read_state(state_path)
    browser = Browser()
    browser.login()
    with browser.request(
        "/api/device/v1/releases/latest",
        bearer=str(state["device_secret"]),
        credential_version=_state_int(state, "credential_version"),
    ) as response:
        if json.load(response).get("release_id") != RELEASE_ID:
            raise RuntimeError("Device Secret or Release assignment did not persist")
    with browser.request(f"/api/devices/{state['device_id']}/queue") as response:
        queue = json.load(response)
    if not queue.get("items") or queue["items"][0]["status"] != "DISPLAYED":
        raise RuntimeError("Queue reference did not persist")
    with browser.request("/api/v1/settings/export") as response:
        if json.load(response)["settings"].get("general.timezone") != "UTC":
            raise RuntimeError("setting did not survive restart")


def _offline_verify(state_path: Path, data_dir: Path) -> None:
    state = _read_state(state_path)
    connection = sqlite3.connect(data_dir / "inktime.db")
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite integrity check failed")
        if (
            int(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])
            != EXPECTED_MIGRATION_VERSION
        ):
            raise RuntimeError("migration version changed")
        if (
            connection.execute("SELECT value_json FROM settings WHERE key='general.timezone'").fetchone()[0]
            != '"UTC"'
        ):
            raise RuntimeError("restored setting mismatch")
        queue = connection.execute(
            "SELECT status FROM device_content_queue_items WHERE device_id=? AND release_id=?",
            (state["device_id"], RELEASE_ID),
        ).fetchone()
        if queue is None or queue[0] != "DISPLAYED":
            raise RuntimeError("offline Queue verification failed")
    finally:
        connection.close()
    if sha256((data_dir / "session.key").read_bytes()).hexdigest() != state["session_key_sha256"]:
        raise RuntimeError("Session/encryption key changed unexpectedly")


def _mutate(data_dir: Path) -> None:
    connection = sqlite3.connect(data_dir / "inktime.db")
    try:
        connection.execute("UPDATE settings SET value_json='\"Asia/Taipei\"' WHERE key='general.timezone'")
        connection.commit()
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Trusted-LAN Compose persistence gate")
    parser.add_argument(
        "phase", choices=("initialize", "seed-release", "exercise", "verify", "offline-verify", "mutate")
    )
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if args.phase == "initialize":
        _initialize(args.state)
    elif args.phase == "exercise":
        _exercise(args.state)
    elif args.phase == "verify":
        _verify(args.state)
    else:
        if args.data_dir is None:
            parser.error(f"{args.phase} requires --data-dir")
        data_dir = args.data_dir.resolve()
        if args.phase == "seed-release":
            _seed_release(args.state, data_dir)
        elif args.phase == "offline-verify":
            _offline_verify(args.state, data_dir)
        else:
            _mutate(data_dir)
    print(f"LAN production gate phase passed: {args.phase}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
