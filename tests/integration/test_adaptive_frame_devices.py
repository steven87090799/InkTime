from __future__ import annotations

from tests.conftest import create_admin, csrf, login


def test_device_adaptive_settings_are_independent_and_old_devices_fall_back(client, app):
    create_admin(app)
    login(client)
    devices = app.extensions["inktime_device_repository"]
    old_id, _ = devices.create("舊裝置")
    portrait_id, _ = devices.create("直向回憶")
    landscape_id, _ = devices.create("橫向回憶")
    for device_id, orientation in ((portrait_id, "portrait"), (landscape_id, "landscape")):
        response = client.patch(
            f"/api/v1/devices/{device_id}",
            json={
                "name": "直向回憶" if orientation == "portrait" else "橫向回憶",
                "enabled": True,
                "timezone": "Asia/Taipei",
                "schedule": "08:00",
                "rotation": 0,
                "panel_profile": "safe_4c",
                "frame_orientation": orientation,
                "layout_mode": "adaptive_memory",
                "fit_mode": "cover",
            },
            headers={"X-CSRF-Token": csrf(client)},
        )
        assert response.status_code == 200
    old = devices.get(old_id)
    portrait = devices.get(portrait_id)
    landscape = devices.get(landscape_id)
    assert old["frame_orientation"] is None
    assert old["layout_mode"] is None
    assert portrait["frame_orientation"] == "portrait"
    assert landscape["frame_orientation"] == "landscape"
    assert portrait["layout_mode"] == landscape["layout_mode"] == "adaptive_memory"


def test_device_adaptive_settings_validate_known_values(client, app):
    create_admin(app)
    login(client)
    device_id, _ = app.extensions["inktime_device_repository"].create("相框")
    response = client.patch(
        f"/api/v1/devices/{device_id}",
        json={
            "name": "相框", "enabled": True, "timezone": "Asia/Taipei", "schedule": "08:00",
            "rotation": 0, "panel_profile": "safe_4c", "frame_orientation": "diagonal",
        },
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 400
    assert "相框方向" in response.get_json()["message"]
