import pytest

from tests.conftest import create_admin, csrf, login
from tests.integration.test_jobs import add_photos


@pytest.mark.parametrize(("x", "y"), [(0.0, 1.0), (0.75, 0.25)])
def test_crop_api_returns_committed_effective_focus_and_restores_zero_auto_focus(client, app, x, y):
    create_admin(app)
    login(client)
    photo_id = add_photos(app, 1)[0]
    database = app.extensions["inktime_database"]
    with database.transaction() as connection:
        connection.execute("UPDATE photos SET crop_focus_x=0,crop_focus_y=1 WHERE id=?", (photo_id,))
    manual = client.patch(f"/api/v1/photos/{photo_id}/crop", json={"mode": "manual", "x": x, "y": y}, headers={"X-CSRF-Token": csrf(client)})
    assert manual.status_code == 200
    assert manual.get_json()["crop"] == {"photo_id": photo_id, "focus_x": x, "focus_y": y, "auto_x": 0.0, "auto_y": 1.0, "manual": True}
    restored = client.patch(f"/api/v1/photos/{photo_id}/crop", json={"mode": "auto"}, headers={"X-CSRF-Token": csrf(client)})
    assert restored.status_code == 200
    assert restored.get_json()["crop"] == {"photo_id": photo_id, "focus_x": 0.0, "focus_y": 1.0, "auto_x": 0.0, "auto_y": 1.0, "manual": False}
    with database.session() as connection:
        assert tuple(connection.execute("SELECT crop_manual_x,crop_manual_y FROM photos WHERE id=?", (photo_id,)).fetchone()) == (None, None)
