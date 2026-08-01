from __future__ import annotations

import pytest

from tests.conftest import create_admin, csrf, login


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_retention_dry_run_rejects_non_boolean(client, app, value):
    create_admin(app)
    login(client)

    response = client.post(
        "/api/retention/run",
        json={"dry_run": value},
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 400
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM data_cleanup_runs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"enabled": "false"},
        {"generate_preview": 1},
        {"daily_max_runs": True},
        {"daily_max_runs": 1.5},
        {"daily_max_runs": "10"},
        {"preview_retention_days": 0},
    ],
)
def test_shadow_config_rejects_ambiguous_json_scalars(client, app, payload):
    create_admin(app)
    login(client)
    before = app.extensions["inktime_resilience_repository"].shadow_config()

    response = client.put(
        "/api/shadow/config",
        json=payload,
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 400
    after = app.extensions["inktime_resilience_repository"].shadow_config()
    assert after == before
