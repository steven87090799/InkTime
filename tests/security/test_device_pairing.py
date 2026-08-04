from __future__ import annotations

import json

from tests.conftest import create_admin, csrf, login


PAIRING_PATH = "/api/device/v1/pairing/request"
CLAIM_PATH = "/api/device/v1/pairing/claim"


def _pairing_payload(device_id: str = "esp32-contract-test") -> dict:
    return {
        "device_id": device_id,
        "pairing_nonce": "nonce-for-contract-test-0123456789",
        "firmware_identity": "ESP32-S3-PhotoPainter",
        "firmware_version": "2.6.0",
        "panel_profile": "safe_4c",
        "capabilities": {"automatic_pairing": True, "ab_credential_store": True},
    }


def _approve(client, pairing_id: str, pairing_code: str, *, include_csrf: bool = True):
    headers = {"Content-Type": "application/json"}
    if include_csrf:
        headers["X-CSRF-Token"] = csrf(client)
    return client.post(
        f"/api/v1/device-pairing/{pairing_id}/approve",
        json={"pairing_code": pairing_code},
        headers=headers,
    )


def test_pairing_request_approval_claim_and_versioned_auth_are_one_time(client, app):
    payload = _pairing_payload()
    requested = client.post(PAIRING_PATH, json=payload)
    assert requested.status_code == 201
    response_body = requested.get_json()
    pairing_id = response_body["pairing_id"]
    pairing_code = response_body["pairing_code"]
    assert len(pairing_code) == 6 and pairing_code.isdigit()
    assert "device_secret" not in response_body

    pending_claim = client.post(
        CLAIM_PATH,
        json={"pairing_id": pairing_id, "pairing_nonce": payload["pairing_nonce"]},
    )
    assert pending_claim.status_code == 202
    assert pending_claim.headers["Retry-After"] == "3"

    with app.extensions["inktime_database"].session() as connection:
        request_row = connection.execute(
            "SELECT pairing_code_hash,pairing_nonce_hash,pairing_code_ciphertext FROM device_pairing_requests WHERE id=?",
            (pairing_id,),
        ).fetchone()
        device_row = connection.execute(
            "SELECT auth_mode,pairing_state,device_secret_hash FROM devices WHERE id=?",
            (payload["device_id"],),
        ).fetchone()
    assert request_row["pairing_code_hash"] != pairing_code
    assert request_row["pairing_nonce_hash"] != payload["pairing_nonce"]
    assert pairing_code.encode("ascii") != bytes(request_row["pairing_code_ciphertext"])
    assert device_row["auth_mode"] == "automatic"
    assert device_row["pairing_state"] == "pairing_pending"
    assert device_row["device_secret_hash"] is None

    create_admin(app)
    login(client)
    pending_page = client.get("/api/v1/device-pairing/pending")
    assert pending_page.status_code == 200
    assert pairing_code in json.dumps(pending_page.get_json(), ensure_ascii=False)

    approved = _approve(client, pairing_id, pairing_code)
    assert approved.status_code == 200
    claimed = client.post(
        CLAIM_PATH,
        json={"pairing_id": pairing_id, "pairing_nonce": payload["pairing_nonce"]},
    )
    assert claimed.status_code == 200
    claim_body = claimed.get_json()
    secret = claim_body["device_secret"]
    version = claim_body["credential_version"]
    assert secret.startswith("ids_")
    assert isinstance(version, int) and version >= 1

    replay = client.post(
        CLAIM_PATH,
        json={"pairing_id": pairing_id, "pairing_nonce": payload["pairing_nonce"]},
    )
    assert replay.status_code == 409

    missing_version = client.post(
        "/api/device/v1/status",
        json={},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert missing_version.status_code == 401
    authenticated = client.post(
        "/api/device/v1/status",
        json={},
        headers={
            "Authorization": f"Bearer {secret}",
            "X-InkTime-Credential-Version": str(version),
        },
    )
    assert authenticated.status_code == 200

    with app.extensions["inktime_database"].session() as connection:
        device_row = connection.execute(
            "SELECT device_secret_hash,pairing_state,credential_version FROM devices WHERE id=?",
            (payload["device_id"],),
        ).fetchone()
        audit_text = json.dumps(
            [
                dict(row)
                for row in connection.execute(
                    "SELECT message,details_json FROM device_events WHERE device_id=?",
                    (payload["device_id"],),
                ).fetchall()
            ],
            ensure_ascii=False,
        )
    assert device_row["device_secret_hash"] != secret
    assert device_row["pairing_state"] == "paired"
    assert device_row["credential_version"] == version
    assert secret not in audit_text
    assert pairing_code not in audit_text

    revoked = client.post(
        f"/api/v1/devices/{payload['device_id']}/revoke-credential",
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert revoked.status_code == 200
    after_revoke = client.post(
        "/api/device/v1/status",
        json={},
        headers={
            "Authorization": f"Bearer {secret}",
            "X-InkTime-Credential-Version": str(version),
        },
    )
    assert after_revoke.status_code == 401
    permission_before_repair = client.get(
        "/api/device/v1/pairing/repair-permission",
        headers={
            "Authorization": f"Bearer {secret}",
            "X-InkTime-Credential-Version": str(version),
        },
    )
    assert permission_before_repair.status_code == 401
    repair_enabled = client.post(
        f"/api/v1/devices/{payload['device_id']}/enable-repair",
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert repair_enabled.status_code == 200
    permission_after_repair = client.get(
        "/api/device/v1/pairing/repair-permission",
        headers={
            "Authorization": f"Bearer {secret}",
            "X-InkTime-Credential-Version": str(version),
        },
    )
    assert permission_after_repair.status_code == 200


def test_pairing_code_attempt_limit_and_admin_csrf_are_enforced(client, app):
    requested = client.post(PAIRING_PATH, json=_pairing_payload("esp32-attempt-limit"))
    assert requested.status_code == 201
    body = requested.get_json()
    wrong_code = "000000" if body["pairing_code"] != "000000" else "999999"

    create_admin(app)
    login(client)
    csrf_missing = _approve(client, body["pairing_id"], wrong_code, include_csrf=False)
    assert csrf_missing.status_code == 403
    for _ in range(5):
        response = _approve(client, body["pairing_id"], wrong_code)
        assert response.status_code == 403

    claim = client.post(
        CLAIM_PATH,
        json={"pairing_id": body["pairing_id"], "pairing_nonce": "nonce-for-contract-test-0123456789"},
    )
    assert claim.status_code == 403


def test_stock_compatibility_never_enters_automatic_pairing(client, app):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "Stock PhotoPainter", delivery_mode="stock_compat"
    )
    response = client.post(PAIRING_PATH, json=_pairing_payload(device_id))
    assert response.status_code == 409
    with app.extensions["inktime_database"].session() as connection:
        device = connection.execute(
            "SELECT auth_mode,pairing_state FROM devices WHERE id=?", (device_id,)
        ).fetchone()
        request_count = connection.execute(
            "SELECT COUNT(*) FROM device_pairing_requests WHERE device_id=?", (device_id,)
        ).fetchone()[0]
    assert device["auth_mode"] == "stock"
    assert device["pairing_state"] == "paired"
    assert request_count == 0
