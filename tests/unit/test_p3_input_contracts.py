from flask import Flask
import pytest

from inktime.app.api.device_auth import authenticate_device_request
from inktime.app.core.json_values import JsonScalarError, json_float, optional_json_float
from inktime.app.services.device_pairing import DevicePairingError, DevicePairingService
from tests.conftest import create_admin, csrf, login


@pytest.mark.parametrize("value", [10**400, -(10**400), float("inf"), float("nan"), True, None])
def test_float_helpers_reject_unrepresentable_or_ambiguous_values(value):
    with pytest.raises(JsonScalarError):
        json_float({"price": value}, "price", minimum=0, maximum=100)
    with pytest.raises(JsonScalarError):
        optional_json_float({"price": value}, "price", minimum=0, maximum=100)


def test_pricing_and_numeric_settings_reject_overflow_without_writes(client, app):
    admin = create_admin(app)
    login(client)
    headers = {"X-CSRF-Token": csrf(client), "X-InkTime-Confirm-Risk": "true"}
    providers = app.extensions["inktime_provider_repository"]
    providers.save({"id": "p3-price", "name": "P3 price", "base_url": "https://example.invalid/v1", "enabled": False}, admin)
    price = {"model": "fixture-model", "input_per_million": 1, "cached_input_per_million": 0.5, "output_per_million": 2}
    for field in price.keys() - {"model"}:
        response = client.post("/api/v1/providers/p3-price/pricing", json=price | {field: 10**400}, headers=headers)
        assert response.status_code == 400
    with pytest.raises(ValueError):
        providers.save_pricing("p3-price", price | {"input_per_million": 10**400})
    assert providers.pricing("p3-price") == {}
    settings = app.extensions["inktime_settings_repository"]
    before = settings.get("budget.daily_stop")
    assert client.post("/api/v1/settings", json={"budget.daily_stop": 10**400}, headers=headers).status_code == 400
    assert settings.get("budget.daily_stop") == before
    assert client.post("/api/v1/providers/p3-price/pricing", json=price, headers=headers).status_code == 200


def test_activity_cursor_int64_bounds_and_ascii_validation(client, app):
    create_admin(app)
    login(client)
    for cursor in ("0", str(2**63 - 1), ":".join([str(2**63 - 1)] * 4)):
        assert client.get("/api/v1/activity", query_string={"after": cursor}).status_code == 200
    for cursor in (str(2**63), "0:0:0:" + str(2**63), "1" * 80, "²", "١", "-1"):
        response = client.get("/api/v1/activity", query_string={"after": cursor})
        assert response.status_code == 400


def test_device_timezone_rejects_invalid_keys_and_keeps_valid_names(client, app):
    create_admin(app)
    login(client)
    headers = {"X-CSRF-Token": csrf(client)}
    for timezone in ("../Asia/Taipei", "/Asia/Taipei", "Invalid/Not_A_Zone", "Asia/../Taipei"):
        assert client.post("/api/v1/devices", json={"name": "P3 device", "timezone": timezone}, headers=headers).status_code == 400
        with pytest.raises(DevicePairingError):
            DevicePairingService._normalize_config({"timezone": timezone}, fallback_name="P3 device")
    result = DevicePairingService._normalize_config({"timezone": "Asia/Taipei"}, fallback_name="P3 device")
    assert result["timezone"] == "Asia/Taipei"
    assert client.post("/api/v1/devices", json={"name": "P3 device", "timezone": "Asia/Taipei"}, headers=headers).status_code == 201


def test_credential_version_invalid_values_never_reach_repository():
    calls = []
    class Repository:
        def authenticate(self, token, address, version, **kwargs):
            calls.append(version)
            return {"id": "fixture-device"}
    app = Flask(__name__)
    @app.get("/device")
    def device():
        return authenticate_device_request(repository=Repository())
    client = app.test_client()
    for version in ("²", "١", "１", "", "1" * 13, "-1", " 1"):
        assert client.get("/device", headers={"Authorization": "Bearer fixture", "X-InkTime-Credential-Version": version}).status_code == 401
    assert calls == []
    for version in ("1", "0001"):
        assert client.get("/device", headers={"Authorization": "Bearer fixture", "X-InkTime-Credential-Version": version}).status_code == 200
    assert calls == [1, 1]


def test_csrf_non_ascii_and_long_values_fail_before_mutation(client, app):
    create_admin(app)
    login(client)
    token = csrf(client)
    settings = app.extensions["inktime_settings_repository"]
    original = settings.get("security.session_minutes")
    for value in ("²", "錯誤", "x" * 200, "wrong-ascii", ""):
        response = client.post("/api/v1/settings", json={"security.session_minutes": 31}, headers={"X-CSRF-Token": value})
        assert response.status_code == 403
        response = client.post("/api/v1/settings", data={"csrf_token": value, "security.session_minutes": "31"})
        assert response.status_code == 403
        assert settings.get("security.session_minutes") == original
    assert client.post("/api/v1/settings", json={"security.session_minutes": 31},
                       headers={"X-CSRF-Token": token, "X-InkTime-Confirm-Risk": "true"}).status_code == 200


def test_optional_schedule_windows_reject_non_strings_without_changes(client, app):
    create_admin(app)
    login(client)
    headers = {"X-CSRF-Token": csrf(client)}
    for field in ("window_start", "window_end"):
        before = client.get("/api/v1/schedules").get_json()
        for value in ([], {}, True, 8, "25:00"):
            assert client.patch("/api/v1/schedules/incremental_scan", json={field: value}, headers=headers).status_code == 400
            assert client.get("/api/v1/schedules").get_json() == before
        for value in (None, "", "02:00"):
            assert client.patch("/api/v1/schedules/incremental_scan", json={field: value}, headers=headers).status_code == 200
