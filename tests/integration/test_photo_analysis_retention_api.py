from __future__ import annotations

from tests.conftest import create_admin, csrf, login


def test_photo_analysis_retention_defaults_to_dry_run_and_requires_confirmation(client, app):
    create_admin(app)
    login(client)
    headers = {"X-CSRF-Token": csrf(client)}

    preview = client.post(
        "/api/v1/maintenance/photo-analysis-retention",
        json={},
        headers=headers,
    )
    assert preview.status_code == 200
    assert preview.get_json()["dry_run"] is True
    assert preview.get_json()["candidate_rows"] == 0

    rejected = client.post(
        "/api/v1/maintenance/photo-analysis-retention",
        json={"dry_run": False},
        headers=headers,
    )
    assert rejected.status_code == 409

    applied = client.post(
        "/api/v1/maintenance/photo-analysis-retention",
        json={
            "dry_run": False,
            "batch_size": 200,
            "confirmation": "DELETE_UNREFERENCED_PHOTO_ANALYSIS",
            "expected_inventory_digest": preview.get_json()["inventory_digest"],
        },
        headers=headers,
    )
    assert applied.status_code == 200
    assert applied.get_json()["dry_run"] is False
    assert applied.get_json()["deleted_rows"] == 0
    assert applied.get_json()["complete"] is True
