from __future__ import annotations

from inktime.app.domain.photopainter.offline_schedule import OFFLINE_PREPARE_BOOTSTRAP_AT
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
            "name": "相框",
            "enabled": True,
            "timezone": "Asia/Taipei",
            "schedule": "08:00",
            "rotation": 0,
            "panel_profile": "safe_4c",
            "frame_orientation": "diagonal",
        },
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 400
    assert "相框方向" in response.get_json()["message"]


def test_device_render_override_bumps_only_config_version_and_replans_offline(client, app):
    create_admin(app)
    login(client)
    devices = app.extensions["inktime_device_repository"]
    device_id, _ = devices.create(
        "離線 render override",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
    )
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            "UPDATE devices SET next_offline_prepare_at=? WHERE id=?",
            ("2099-01-01T00:00:00+00:00", device_id),
        )
    before = devices.get(device_id)

    response = client.patch(
        f"/api/v1/devices/{device_id}",
        json={
            "frame_orientation": "landscape",
            "layout_mode": "adaptive_memory",
            "fit_mode": "cover",
        },
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 200
    after = devices.get(device_id)
    assert int(after["config_version"]) == int(before["config_version"]) + 1
    assert int(after["offline_schedule_version"]) == int(before["offline_schedule_version"])
    assert after["next_offline_prepare_at"] == OFFLINE_PREPARE_BOOTSTRAP_AT
    assert (
        after["frame_orientation"],
        after["layout_mode"],
        after["fit_mode"],
    ) == ("landscape", "adaptive_memory", "cover")

    repeated = client.patch(
        f"/api/v1/devices/{device_id}",
        json={
            "frame_orientation": "landscape",
            "layout_mode": "adaptive_memory",
            "fit_mode": "cover",
        },
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert repeated.status_code == 200
    unchanged = devices.get(device_id)
    assert int(unchanged["config_version"]) == int(after["config_version"])
    assert int(unchanged["offline_schedule_version"]) == int(after["offline_schedule_version"])
