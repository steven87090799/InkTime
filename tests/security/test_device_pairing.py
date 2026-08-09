from __future__ import annotations

import json

import pytest

from inktime.app.services.device_pairing import DevicePairingError
from tests.conftest import create_admin, csrf, login


PAIRING_PATH = "/api/device/v1/pairing/request"
CLAIM_PATH = "/api/device/v1/pairing/claim"
CONFIRM_PATH = "/api/device/v1/pairing/confirm"


def _pairing_payload(
    device_id: str = "esp32-contract-test",
    *,
    nonce: str | None = None,
    capabilities: dict | None = None,
) -> dict:
    return {
        "device_id": device_id,
        "pairing_nonce": nonce or "nonce-for-contract-test-0123456789",
        "firmware_identity": "ESP32-S3-PhotoPainter",
        "firmware_version": "2.6.0",
        "panel_profile": "safe_4c",
        "capabilities": capabilities
        or {"automatic_pairing": True, "ab_credential_store": True},
    }


@pytest.mark.parametrize(
    ("schedule_times", "minimum_gap_minutes", "expected"),
    [
        (["08:00"], 60, ["08:00"]),
        (["08:00", "12:00", "20:00"], 60, ["08:00", "12:00", "20:00"]),
        (["08:00", "08:30"], 30, ["08:00", "08:30"]),
    ],
)
def test_pairing_schedule_values_accept_configured_gap(app, schedule_times, minimum_gap_minutes, expected):
    service = app.extensions["inktime_device_pairing_service"]
    config = service._normalize_config(
        {
            "name": "LAN Gate Device",
            "panel_profile": "safe_4c",
            "schedule_times": schedule_times,
            "minimum_schedule_gap_minutes": minimum_gap_minutes,
        },
        fallback_name="LAN Gate Device",
    )
    assert config["schedule_times"] == expected
    assert config["minimum_schedule_gap_minutes"] == minimum_gap_minutes


def test_pairing_unknown_capability_keeps_legacy_12_slot_boundary(app):
    service = app.extensions["inktime_device_pairing_service"]
    schedule_times = [f"{hour:02d}:00" for hour in range(13)]
    with pytest.raises(DevicePairingError):
        service._normalize_config(
            {
                "name": "Legacy PhotoPainter",
                "panel_profile": "safe_4c",
                "schedule_times": schedule_times,
            },
            fallback_name="Legacy PhotoPainter",
        )
    config = service._normalize_config(
        {
            "name": "New PhotoPainter",
            "panel_profile": "safe_4c",
            "schedule_times": schedule_times,
        },
        fallback_name="New PhotoPainter",
        offline_schedule_max_slots=24,
    )
    assert len(config["schedule_times"]) == 13


def test_pairing_schedule_values_preserve_fixed_daily_sync_policy(app):
    service = app.extensions["inktime_device_pairing_service"]
    config = service._normalize_config(
        {
            "name": "LAN Gate Device",
            "panel_profile": "safe_4c",
            "schedule_times": ["08:00", "20:00"],
            "minimum_schedule_gap_minutes": 30,
            "sync_strategy": "fixed_daily",
            "sync_time": "07:30",
        },
        fallback_name="LAN Gate Device",
    )
    assert config["minimum_schedule_gap_minutes"] == 30
    assert config["sync_strategy"] == "fixed_daily"
    assert config["sync_time"] == "07:30"


def test_pairing_schedule_values_use_default_fallback(app):
    service = app.extensions["inktime_device_pairing_service"]
    config = service._normalize_config(
        {"name": "LAN Gate Device", "panel_profile": "safe_4c"},
        fallback_name="LAN Gate Device",
    )
    assert config["schedule_times"] == ["08:00"]


def test_pairing_approval_merges_partial_management_payload(client, app):
    payload = _pairing_payload("esp32-partial-config")
    requested = client.post(PAIRING_PATH, json=payload)
    assert requested.status_code == 201
    response_body = requested.get_json()
    pairing_id = response_body["pairing_id"]
    pairing_code = response_body["pairing_code"]

    with app.extensions["inktime_database"].transaction() as connection:
        stored = json.loads(
            str(
                connection.execute(
                    "SELECT config_json FROM device_pairing_requests WHERE id=?",
                    (pairing_id,),
                ).fetchone()[0]
            )
        )
        stored.update(
            {
                "delivery_mode": "inktime_offline_schedule",
                "schedule_times": ["08:00", "08:30"],
                "minimum_schedule_gap_minutes": 30,
                "sync_strategy": "fixed_daily",
                "sync_time": "07:30",
            }
        )
        connection.execute(
            "UPDATE device_pairing_requests SET config_json=? WHERE id=?",
            (json.dumps(stored, ensure_ascii=False, separators=(",", ":")), pairing_id),
        )

    create_admin(app)
    login(client)
    approved = _approve(client, pairing_id, pairing_code)
    assert approved.status_code == 200
    claimed = client.post(
        CLAIM_PATH,
        json={"pairing_id": pairing_id, "pairing_nonce": payload["pairing_nonce"]},
    )
    assert claimed.status_code == 200
    claim_body = claimed.get_json()
    confirm_body = {
        "pairing_id": pairing_id,
        "device_id": payload["device_id"],
        "pairing_nonce": payload["pairing_nonce"],
    }
    assert _confirm(
        client,
        confirm_body,
        claim_body["device_secret"],
        claim_body["credential_version"],
    ).status_code == 200

    with app.extensions["inktime_database"].session() as connection:
        device = connection.execute(
            "SELECT delivery_mode,schedule_times_json,minimum_schedule_gap_minutes,sync_strategy,sync_time "
            "FROM devices WHERE id=?",
            (payload["device_id"],),
        ).fetchone()
    assert device["delivery_mode"] == "inktime_offline_schedule"
    assert json.loads(str(device["schedule_times_json"])) == ["08:00"]
    assert device["minimum_schedule_gap_minutes"] == 30
    assert device["sync_strategy"] == "fixed_daily"
    assert device["sync_time"] == "07:30"


def test_repair_pairing_preserves_existing_schedule_policy_on_partial_approval(client, app):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "Repair PhotoPainter",
        auth_mode="automatic",
        delivery_mode="inktime_offline_schedule",
        schedule="08:00",
        schedule_times=["08:00", "08:30", "09:30"],
        minimum_schedule_gap_minutes=30,
        sync_strategy="fixed_daily",
        sync_time="07:30",
    )
    create_admin(app)
    login(client)
    repair_enabled = client.post(
        f"/api/v1/devices/{device_id}/enable-repair",
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert repair_enabled.status_code == 200

    payload = _pairing_payload(
        device_id,
        nonce="nonce-for-existing-repair-preservation-0123456789",
    )
    payload["device_name"] = "Repaired PhotoPainter"
    payload["panel_profile"] = "gdep073e01_6c"
    requested = client.post(PAIRING_PATH, json=payload)
    assert requested.status_code == 201
    requested_body = requested.get_json()
    with app.extensions["inktime_database"].session() as connection:
        stored = json.loads(
            str(
                connection.execute(
                    "SELECT config_json FROM device_pairing_requests WHERE id=?",
                    (requested_body["pairing_id"],),
                ).fetchone()[0]
            )
        )
        before_repair = connection.execute(
            "SELECT name,panel_profile FROM devices WHERE id=?", (device_id,)
        ).fetchone()
    assert stored["delivery_mode"] == "inktime_offline_schedule"
    assert stored["schedule_times"] == ["08:00", "08:30", "09:30"]
    assert stored["minimum_schedule_gap_minutes"] == 30
    assert stored["sync_strategy"] == "fixed_daily"
    assert stored["sync_time"] == "07:30"
    assert before_repair["name"] == "Repair PhotoPainter"
    assert before_repair["panel_profile"] == "safe_4c"

    approved = client.post(
        f"/api/v1/device-pairing/{requested_body['pairing_id']}/approve",
        json={
            "pairing_code": requested_body["pairing_code"],
            "device_config": {
                "name": "Repaired PhotoPainter",
                "panel_profile": "safe_4c",
                "timezone": "Asia/Taipei",
                "schedule": "08:00",
                "schedule_times": ["08:00", "08:30", "09:30"],
            },
        },
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert approved.status_code == 200
    claimed = client.post(
        CLAIM_PATH,
        json={
            "pairing_id": requested_body["pairing_id"],
            "pairing_nonce": payload["pairing_nonce"],
        },
    )
    assert claimed.status_code == 200
    claim_body = claimed.get_json()
    confirmed = _confirm(
        client,
        {
            "pairing_id": requested_body["pairing_id"],
            "device_id": device_id,
            "pairing_nonce": payload["pairing_nonce"],
        },
        claim_body["device_secret"],
        claim_body["credential_version"],
    )
    assert confirmed.status_code == 200

    with app.extensions["inktime_database"].session() as connection:
        device = connection.execute(
            "SELECT name,panel_profile,delivery_mode,schedule_times_json,minimum_schedule_gap_minutes,sync_strategy,sync_time "
            "FROM devices WHERE id=?",
            (device_id,),
        ).fetchone()
    assert device["name"] == "Repair PhotoPainter"
    assert device["panel_profile"] == "safe_4c"
    assert device["delivery_mode"] == "inktime_offline_schedule"
    assert json.loads(str(device["schedule_times_json"])) == ["08:00", "08:30", "09:30"]
    assert device["minimum_schedule_gap_minutes"] == 30
    assert device["sync_strategy"] == "fixed_daily"
    assert device["sync_time"] == "07:30"


@pytest.mark.parametrize(
    "schedule_times",
    [
        ["08:00", 123],
        ["08:00", True],
        ["08:00", None],
        ["08:00", 12.5],
        ["08:00", {}],
        ["08:00", []],
    ],
)
def test_pairing_schedule_values_reject_mixed_types(app, schedule_times):
    service = app.extensions["inktime_device_pairing_service"]
    with pytest.raises(DevicePairingError) as error:
        service._normalize_config(
            {
                "name": "LAN Gate Device",
                "panel_profile": "safe_4c",
                "schedule_times": schedule_times,
            },
            fallback_name="LAN Gate Device",
        )
    assert error.value.error_code == "PAIR-004"


@pytest.mark.parametrize(
    "schedule_times",
    [["12:00", "08:00"], ["08:00", "08:00"], []],
)
def test_pairing_schedule_values_reject_invalid_values(app, schedule_times):
    service = app.extensions["inktime_device_pairing_service"]
    with pytest.raises(DevicePairingError) as error:
        service._normalize_config(
            {
                "name": "LAN Gate Device",
                "panel_profile": "safe_4c",
                "schedule_times": schedule_times,
            },
            fallback_name="LAN Gate Device",
        )
    assert error.value.error_code == "PAIR-004"


def test_pairing_accepts_stretch_fill_fit_contract(app):
    service = app.extensions["inktime_device_pairing_service"]
    normalized = service._normalize_config(
        {
            "name": "Stretch Fill Device",
            "panel_profile": "safe_4c",
            "fit_mode": "stretch_fill",
        },
        fallback_name="Stretch Fill Device",
    )
    assert normalized["fit_mode"] == "stretch_fill"


def _approve(
    client,
    pairing_id: str,
    pairing_code: str,
    *,
    include_csrf: bool = True,
    device_config: dict | None = None,
):
    headers = {"Content-Type": "application/json"}
    if include_csrf:
        headers["X-CSRF-Token"] = csrf(client)
    return client.post(
        f"/api/v1/device-pairing/{pairing_id}/approve",
        json={
            "pairing_code": pairing_code,
            "device_config": device_config
            or {
                "name": "測試相框",
                "panel_profile": "safe_4c",
                "timezone": "Asia/Taipei",
                "schedule": "08:00",
                "schedule_times": ["08:00"],
            },
        },
        headers=headers,
    )


def _confirm(client, body: dict, secret: str, version: int):
    return client.post(
        CONFIRM_PATH,
        json=body,
        headers={
            "Authorization": f"Bearer {secret}",
            "X-InkTime-Credential-Version": str(version),
        },
    )


def test_explicit_24_slot_capability_survives_pairing_confirm(client, app):
    schedule_times = [f"{hour:02d}:00" for hour in range(24)]
    payload = _pairing_payload(
        "esp32-24-slot-contract",
        capabilities={
            "automatic_pairing": True,
            "ab_credential_store": True,
            "offline_schedule_max_slots": 24,
        },
    )
    requested = client.post(PAIRING_PATH, json=payload)
    assert requested.status_code == 201
    requested_body = requested.get_json()
    create_admin(app)
    login(client)
    approved = _approve(
        client,
        requested_body["pairing_id"],
        requested_body["pairing_code"],
        device_config={
            "name": "24 Slot PhotoPainter",
            "panel_profile": "safe_4c",
            "timezone": "Asia/Taipei",
            "schedule": "00:00",
            "schedule_times": schedule_times,
            "delivery_mode": "inktime_offline_schedule",
        },
    )
    assert approved.status_code == 200
    claimed = client.post(
        CLAIM_PATH,
        json={"pairing_id": requested_body["pairing_id"], "pairing_nonce": payload["pairing_nonce"]},
    )
    assert claimed.status_code == 200
    claim_body = claimed.get_json()
    confirmed = _confirm(
        client,
        {
            "pairing_id": requested_body["pairing_id"],
            "device_id": payload["device_id"],
            "pairing_nonce": payload["pairing_nonce"],
        },
        claim_body["device_secret"],
        claim_body["credential_version"],
    )
    assert confirmed.status_code == 200

    with app.extensions["inktime_database"].session() as connection:
        device = connection.execute(
            "SELECT offline_schedule_max_slots,offline_schedule_capability_state,schedule_times_json FROM devices WHERE id=?",
            (payload["device_id"],),
        ).fetchone()
    assert device["offline_schedule_max_slots"] == 24
    assert device["offline_schedule_capability_state"] == "confirmed_24"
    assert len(json.loads(str(device["schedule_times_json"]))) == 24


def test_pairing_proves_physical_possession_and_confirm_is_recoverable(client, app):
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
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(device_pairing_requests)")}
        request_row = connection.execute(
            "SELECT pairing_code_hash,pairing_nonce_hash,credential_envelope_ciphertext FROM device_pairing_requests WHERE id=?",
            (pairing_id,),
        ).fetchone()
        device_row = connection.execute(
            "SELECT id FROM devices WHERE id=?", (payload["device_id"],)
        ).fetchone()
    assert "pairing_code_ciphertext" not in columns
    assert request_row["pairing_code_hash"] != pairing_code
    assert request_row["pairing_nonce_hash"] != payload["pairing_nonce"]
    assert request_row["credential_envelope_ciphertext"] is None
    assert device_row is None

    create_admin(app)
    login(client)
    pending_page = client.get("/api/v1/device-pairing/pending")
    assert pending_page.status_code == 200
    assert pairing_code not in json.dumps(pending_page.get_json(), ensure_ascii=False)
    device_page = client.get("/devices")
    assert device_page.status_code == 200
    assert pairing_code not in device_page.get_data(as_text=True)
    assert "pairing-code-input" in device_page.get_data(as_text=True)

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

    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute(
            "SELECT id FROM devices WHERE id=?", (payload["device_id"],)
        ).fetchone() is None
        request_row = connection.execute(
            "SELECT status,credential_envelope_ciphertext FROM device_pairing_requests WHERE id=?",
            (pairing_id,),
        ).fetchone()
    assert request_row["status"] == "credential_issued"
    assert request_row["credential_envelope_ciphertext"] is not None

    retry = client.post(
        CLAIM_PATH,
        json={"pairing_id": pairing_id, "pairing_nonce": payload["pairing_nonce"]},
    )
    assert retry.status_code == 200
    assert retry.get_json()["device_secret"] == secret
    assert retry.get_json()["credential_version"] == version

    confirm_body = {
        "pairing_id": pairing_id,
        "device_id": payload["device_id"],
        "pairing_nonce": payload["pairing_nonce"],
    }
    assert client.post(CONFIRM_PATH, json=confirm_body).status_code == 401
    confirmed = _confirm(client, confirm_body, secret, version)
    assert confirmed.status_code == 200
    assert confirmed.get_json()["status"] == "confirmed"

    with app.extensions["inktime_database"].session() as connection:
        device_row = connection.execute(
            "SELECT device_secret_hash,pairing_state,credential_version FROM devices WHERE id=?",
            (payload["device_id"],),
        ).fetchone()
        request_row = connection.execute(
            "SELECT status,credential_envelope_ciphertext FROM device_pairing_requests WHERE id=?",
            (pairing_id,),
        ).fetchone()
        audit_text = json.dumps(
            [dict(row) for row in connection.execute(
                "SELECT message,details_json FROM activity_events WHERE source='device_pairing'"
            ).fetchall()],
            ensure_ascii=False,
        )
    assert device_row["device_secret_hash"] != secret
    assert device_row["pairing_state"] == "paired"
    assert device_row["credential_version"] == version
    assert request_row["status"] == "confirmed"
    assert request_row["credential_envelope_ciphertext"] is None
    assert secret not in audit_text
    assert pairing_code not in audit_text

    confirmed_retry = _confirm(client, confirm_body, secret, version)
    assert confirmed_retry.status_code == 200
    assert confirmed_retry.get_json()["status"] == "already_confirmed"
    assert client.post(CLAIM_PATH, json={
        "pairing_id": pairing_id,
        "pairing_nonce": payload["pairing_nonce"],
    }).status_code == 409

    authenticated = client.post(
        "/api/device/v1/status",
        json={},
        headers={
            "Authorization": f"Bearer {secret}",
            "X-InkTime-Credential-Version": str(version),
        },
    )
    assert authenticated.status_code == 200

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

    repair_payload = _pairing_payload(
        payload["device_id"], nonce="nonce-for-existing-repair-reject-0123456789"
    )
    repair_requested = client.post(PAIRING_PATH, json=repair_payload)
    assert repair_requested.status_code == 201
    repair_body = repair_requested.get_json()
    assert _approve(client, repair_body["pairing_id"], repair_body["pairing_code"]).status_code == 200
    repair_claimed = client.post(
        CLAIM_PATH,
        json={
            "pairing_id": repair_body["pairing_id"],
            "pairing_nonce": repair_payload["pairing_nonce"],
        },
    )
    assert repair_claimed.status_code == 200
    repair_secret = repair_claimed.get_json()["device_secret"]
    repair_version = repair_claimed.get_json()["credential_version"]
    repair_rejected = client.post(
        f"/api/v1/device-pairing/{repair_body['pairing_id']}/reject",
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert repair_rejected.status_code == 200

    with app.extensions["inktime_database"].session() as connection:
        repaired_device = connection.execute(
            "SELECT pairing_state,repair_allowed_until FROM devices WHERE id=?",
            (payload["device_id"],),
        ).fetchone()
        repair_request = connection.execute(
            "SELECT status,credential_envelope_ciphertext FROM device_pairing_requests WHERE id=?",
            (repair_body["pairing_id"],),
        ).fetchone()
    assert repaired_device["pairing_state"] == "revoked"
    assert repaired_device["repair_allowed_until"] is None
    assert repair_request["status"] == "rejected"
    assert repair_request["credential_envelope_ciphertext"] is None

    old_after_repair_reject = client.post(
        "/api/device/v1/status",
        json={},
        headers={
            "Authorization": f"Bearer {secret}",
            "X-InkTime-Credential-Version": str(version),
        },
    )
    assert old_after_repair_reject.status_code == 401
    issued_after_repair_reject = client.post(
        "/api/device/v1/status",
        json={},
        headers={
            "Authorization": f"Bearer {repair_secret}",
            "X-InkTime-Credential-Version": str(repair_version),
        },
    )
    assert issued_after_repair_reject.status_code == 401
    permission_after_reject = client.get(
        "/api/device/v1/pairing/repair-permission",
        headers={
            "Authorization": f"Bearer {secret}",
            "X-InkTime-Credential-Version": str(version),
        },
    )
    assert permission_after_reject.status_code == 401
    blocked_request = client.post(
        PAIRING_PATH,
        json=_pairing_payload(
            payload["device_id"], nonce="nonce-before-second-repair-0123456789"
        ),
    )
    assert blocked_request.status_code == 409
    repair_enabled_again = client.post(
        f"/api/v1/devices/{payload['device_id']}/enable-repair",
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert repair_enabled_again.status_code == 200
    permission_after_again = client.get(
        "/api/device/v1/pairing/repair-permission",
        headers={
            "Authorization": f"Bearer {secret}",
            "X-InkTime-Credential-Version": str(version),
        },
    )
    assert permission_after_again.status_code == 200


def test_admin_can_reject_issued_credential_before_confirm(client, app):
    payload = _pairing_payload("esp32-admin-reject")
    requested = client.post(PAIRING_PATH, json=payload)
    assert requested.status_code == 201
    response_body = requested.get_json()
    pairing_id = response_body["pairing_id"]
    pairing_code = response_body["pairing_code"]

    create_admin(app)
    login(client)
    assert _approve(client, pairing_id, pairing_code).status_code == 200
    approved_page = client.get("/devices")
    assert approved_page.status_code == 200
    approved_page_html = approved_page.get_data(as_text=True)
    assert '已核准，等待裝置領取</span><button class="secondary reject-pairing"' in approved_page_html
    claimed = client.post(
        CLAIM_PATH,
        json={"pairing_id": pairing_id, "pairing_nonce": payload["pairing_nonce"]},
    )
    assert claimed.status_code == 200
    claim_body = claimed.get_json()
    secret = claim_body["device_secret"]
    version = claim_body["credential_version"]

    device_page = client.get("/devices")
    assert device_page.status_code == 200
    device_page_html = device_page.get_data(as_text=True)
    assert "等待裝置確認" in device_page_html
    assert "reject-pairing" in device_page_html

    rejected = client.post(
        f"/api/v1/device-pairing/{pairing_id}/reject",
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert rejected.status_code == 200

    with app.extensions["inktime_database"].session() as connection:
        request_row = connection.execute(
            "SELECT status,credential_envelope_ciphertext FROM device_pairing_requests WHERE id=?",
            (pairing_id,),
        ).fetchone()
        device_row = connection.execute(
            "SELECT id FROM devices WHERE id=?", (payload["device_id"],)
        ).fetchone()
    assert request_row["status"] == "rejected"
    assert request_row["credential_envelope_ciphertext"] is None
    assert device_row is None

    confirm_body = {
        "pairing_id": pairing_id,
        "device_id": payload["device_id"],
        "pairing_nonce": payload["pairing_nonce"],
    }
    assert _confirm(client, confirm_body, secret, version).status_code == 410
    authenticated = client.post(
        "/api/device/v1/status",
        json={},
        headers={
            "Authorization": f"Bearer {secret}",
            "X-InkTime-Credential-Version": str(version),
        },
    )
    assert authenticated.status_code == 401


def test_pairing_request_is_idempotent_for_same_nonce_but_not_for_new_nonce(client, app):
    payload = _pairing_payload("esp32-duplicate")
    first = client.post(PAIRING_PATH, json=payload)
    assert first.status_code == 201
    second = client.post(PAIRING_PATH, json=payload)
    assert second.status_code == 200
    assert second.get_json()["pairing_id"] == first.get_json()["pairing_id"]
    assert second.get_json()["pairing_code"] == first.get_json()["pairing_code"]
    assert second.get_json()["request_reused"] is True
    conflict = client.post(
        PAIRING_PATH,
        json=_pairing_payload(payload["device_id"], nonce="a-different-nonce-0123456789"),
    )
    assert conflict.status_code == 409
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM device_pairing_requests WHERE device_id=?",
            (payload["device_id"],),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM devices WHERE id=?", (payload["device_id"],)
        ).fetchone()[0] == 0


def test_pairing_request_replay_recovers_lost_response_with_same_display_code(client, app):
    payload = _pairing_payload("esp32-lost-response")
    first = client.post(PAIRING_PATH, json=payload)
    assert first.status_code == 201
    first_body = first.get_json()
    pairing_id = first_body["pairing_id"]
    pairing_code = first_body["pairing_code"]
    with app.extensions["inktime_database"].session() as connection:
        original = connection.execute(
            "SELECT id,expires_at,pairing_code_hash,pairing_nonce_hash FROM device_pairing_requests WHERE id=?",
            (pairing_id,),
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM device_pairing_requests WHERE device_id=?", (payload["device_id"],)
        ).fetchone()[0] == 1

    replay = client.post(PAIRING_PATH, json=payload)
    assert replay.status_code == 200
    replay_body = replay.get_json()
    assert replay_body["status"] == "pending"
    assert replay_body["pairing_id"] == pairing_id
    assert replay_body["pairing_code"] == pairing_code
    assert replay_body["request_reused"] is True

    with app.extensions["inktime_database"].session() as connection:
        current = connection.execute(
            "SELECT id,expires_at,pairing_code_hash,pairing_nonce_hash FROM device_pairing_requests WHERE id=?",
            (pairing_id,),
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM device_pairing_requests WHERE device_id=?", (payload["device_id"],)
        ).fetchone()[0] == 1
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(device_pairing_requests)")}
    assert current["id"] == original["id"]
    assert current["expires_at"] == original["expires_at"]
    assert current["pairing_code_hash"] == original["pairing_code_hash"]
    assert current["pairing_code_hash"] != pairing_code
    assert current["pairing_nonce_hash"] != payload["pairing_nonce"]
    assert "pairing_code_ciphertext" not in columns

    create_admin(app)
    login(client)
    pending_json = json.dumps(client.get("/api/v1/device-pairing/pending").get_json(), ensure_ascii=False)
    pending_html = client.get("/devices").get_data(as_text=True)
    assert pairing_code not in pending_json
    assert pairing_code not in pending_html


def test_pairing_code_attempt_limit_and_admin_csrf_are_enforced(client, app):
    requested = client.post(PAIRING_PATH, json=_pairing_payload("esp32-attempt-limit"))
    assert requested.status_code == 201
    body = requested.get_json()
    wrong_code = "000000" if body["pairing_code"] != "000000" else "999999"

    create_admin(app)
    login(client)
    assert _approve(client, body["pairing_id"], wrong_code, include_csrf=False).status_code == 403
    for _ in range(5):
        assert _approve(client, body["pairing_id"], wrong_code).status_code == 403

    claim = client.post(
        CLAIM_PATH,
        json={"pairing_id": body["pairing_id"], "pairing_nonce": "nonce-for-contract-test-0123456789"},
    )
    assert claim.status_code == 410


def test_pairing_claim_attempt_limit_is_persisted(client, app):
    payload = _pairing_payload(
        "esp32-claim-attempt-limit",
        nonce="nonce-for-claim-attempt-limit-0123456789",
    )
    requested = client.post(PAIRING_PATH, json=payload)
    assert requested.status_code == 201
    pairing_id = requested.get_json()["pairing_id"]
    wrong_nonce = "wrong-nonce-for-claim-attempts-0123456789"

    for expected_attempts in range(1, 5):
        response = client.post(
            CLAIM_PATH,
            json={"pairing_id": pairing_id, "pairing_nonce": wrong_nonce},
        )
        assert response.status_code == 401
        with app.extensions["inktime_database"].session() as connection:
            row = connection.execute(
                "SELECT claim_attempts,status FROM device_pairing_requests WHERE id=?",
                (pairing_id,),
            ).fetchone()
        assert row["claim_attempts"] == expected_attempts
        assert row["status"] == "pending"

    fifth = client.post(
        CLAIM_PATH,
        json={"pairing_id": pairing_id, "pairing_nonce": wrong_nonce},
    )
    assert fifth.status_code == 401
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT claim_attempts,status FROM device_pairing_requests WHERE id=?",
            (pairing_id,),
        ).fetchone()
    assert row["claim_attempts"] == 5
    assert row["status"] == "rejected"

    correct = client.post(
        CLAIM_PATH,
        json={"pairing_id": pairing_id, "pairing_nonce": payload["pairing_nonce"]},
    )
    assert correct.status_code == 410


def test_pairing_claim_rejection_clears_issued_credential_envelope(client, app):
    payload = _pairing_payload(
        "esp32-claim-envelope-cleanup",
        nonce="nonce-for-claim-envelope-cleanup-0123456789",
    )
    requested = client.post(PAIRING_PATH, json=payload)
    assert requested.status_code == 201
    body = requested.get_json()

    create_admin(app)
    login(client)
    assert _approve(client, body["pairing_id"], body["pairing_code"]).status_code == 200
    issued = client.post(
        CLAIM_PATH,
        json={"pairing_id": body["pairing_id"], "pairing_nonce": payload["pairing_nonce"]},
    )
    assert issued.status_code == 200
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT credential_envelope_ciphertext,credential_envelope_expires_at FROM device_pairing_requests WHERE id=?",
            (body["pairing_id"],),
        ).fetchone()
    assert row["credential_envelope_ciphertext"] is not None
    assert row["credential_envelope_expires_at"] is not None

    wrong_nonce = "wrong-nonce-for-issued-envelope-0123456789"
    for _ in range(5):
        response = client.post(
            CLAIM_PATH,
            json={"pairing_id": body["pairing_id"], "pairing_nonce": wrong_nonce},
        )
        assert response.status_code == 401

    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT claim_attempts,status,credential_envelope_ciphertext,credential_envelope_expires_at FROM device_pairing_requests WHERE id=?",
            (body["pairing_id"],),
        ).fetchone()
    assert row["claim_attempts"] == 5
    assert row["status"] == "rejected"
    assert row["credential_envelope_ciphertext"] is None
    assert row["credential_envelope_expires_at"] is None
    assert client.post(
        CLAIM_PATH,
        json={"pairing_id": body["pairing_id"], "pairing_nonce": payload["pairing_nonce"]},
    ).status_code == 410


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
