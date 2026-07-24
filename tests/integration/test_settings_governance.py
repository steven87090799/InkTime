from __future__ import annotations

import json
import sqlite3

import pytest

from inktime.app.repositories.settings import (
    DEVICE_OVERRIDE_KEYS,
    SETTING_DEFINITIONS,
)
from tests.conftest import create_admin, csrf, login


def _post(client, path: str, payload: dict, *, confirm: bool = False):
    headers = {"X-CSRF-Token": csrf(client)}
    if confirm:
        headers["X-InkTime-Confirm-Risk"] = "true"
    return client.post(
        path,
        json=payload,
        headers=headers,
    )


def test_metadata_is_complete_and_uses_zh_tw_labels(client, app):
    create_admin(app)
    login(client)
    response = client.get("/api/v1/settings/metadata")
    assert response.status_code == 200
    assert response.json["schema_version"] == 1
    required = {
        "key",
        "label_zh_tw",
        "category",
        "description",
        "risk",
        "type",
        "default",
        "min",
        "max",
        "choices",
        "choice_labels",
        "safe_fallback",
        "visibility",
        "advanced",
        "secret",
        "restart_required",
        "effective_scope",
        "cache_impact",
        "reanalysis_impact",
        "rerender_impact",
        "device_override_allowed",
        "dependencies",
        "conflicts",
        "validation_group",
    }
    assert all(required <= set(item) for item in response.json["settings"])
    labels = {item["key"]: item["label_zh_tw"] for item in response.json["settings"]}
    assert labels["analysis.ai_daily_photo_limit"] == "每日 AI 分析照片上限"
    assert all(item["risk"] in {"low", "medium", "high"} for item in response.json["settings"])
    assert all(item["secret"] is False for item in response.json["settings"])


def test_partial_update_only_writes_changed_keys_and_creates_one_snapshot(client, app):
    create_admin(app)
    login(client)
    response = _post(
        client,
        "/api/v1/settings",
        {"analysis.concurrency": 2, "general.timezone": "Asia/Taipei"},
        confirm=True,
    )
    assert response.status_code == 200
    assert response.json["updated"] == 1
    assert response.json["changed_keys"] == ["analysis.concurrency"]
    assert response.json["snapshot_id"]
    with app.extensions["inktime_database"].session() as connection:
        history = connection.execute("SELECT key FROM setting_history").fetchall()
        snapshots = connection.execute("SELECT changed_keys_json FROM settings_snapshots").fetchall()
    assert [row["key"] for row in history] == ["analysis.concurrency"]
    assert json.loads(snapshots[0]["changed_keys_json"]) == ["analysis.concurrency"]


def test_unknown_key_rejects_entire_partial_update(client, app):
    create_admin(app)
    login(client)
    response = _post(
        client,
        "/api/v1/settings",
        {"analysis.concurrency": 2, "danger.shell": "rm"},
    )
    assert response.status_code == 400
    repository = app.extensions["inktime_settings_repository"]
    assert repository.get("analysis.concurrency") == 1
    assert repository.snapshots() == []


def test_cross_field_validation_uses_current_plus_partial_update(client, app):
    create_admin(app)
    login(client)
    invalid = _post(
        client,
        "/api/v1/settings/preview",
        {"analysis.caption_min_chars": 221},
    )
    assert invalid.status_code == 200
    assert invalid.json["valid"] is False
    assert "min ≤ target ≤ max" in invalid.json["validation_errors"][0]
    assert app.extensions["inktime_settings_repository"].get(
        "analysis.caption_min_chars"
    ) == 120

    valid = client.post(
        "/api/v1/settings",
        json={
            "analysis.caption_min_chars": 180,
            "analysis.caption_target_chars": 200,
            "analysis.caption_max_chars": 240,
        },
        headers={
            "X-CSRF-Token": csrf(client),
            "X-InkTime-Confirm-Risk": "true",
        },
    )
    assert valid.status_code == 200
    assert valid.json["updated"] == 3


def test_high_risk_change_requires_preview_confirmation(client, app):
    create_admin(app)
    login(client)
    blocked = _post(
        client,
        "/api/v1/settings",
        {"analysis.ai_mode": "full_library"},
    )
    assert blocked.status_code == 409
    assert app.extensions["inktime_settings_repository"].get("analysis.ai_mode") == "top_candidates"
    confirmed = client.post(
        "/api/v1/settings",
        json={"analysis.ai_mode": "full_library"},
        headers={
            "X-CSRF-Token": csrf(client),
            "X-InkTime-Confirm-Risk": "true",
        },
    )
    assert confirmed.status_code == 200


def test_transaction_failure_leaves_no_setting_snapshot_or_history(app):
    repository = app.extensions["inktime_settings_repository"]
    database = app.extensions["inktime_database"]
    with database.session() as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_timezone_update
            BEFORE UPDATE ON settings
            WHEN NEW.key='general.timezone' AND NEW.value_json='"UTC"'
            BEGIN SELECT RAISE(ABORT, 'test rollback'); END
            """
        )
    with pytest.raises(sqlite3.IntegrityError):
        repository.update_many(
            {"analysis.concurrency": 2, "general.timezone": "UTC"},
            changed_by="test",
            source_ip="127.0.0.1",
        )
    assert repository.get("analysis.concurrency") == 1
    assert repository.get("general.timezone") == "Asia/Taipei"
    with database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM settings_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM setting_history").fetchone()[0] == 0


def test_private_locations_are_redacted_from_snapshot_and_export(client, app):
    create_admin(app)
    login(client)
    response = _post(
        client,
        "/api/v1/settings",
        {
            "home_latitude": 24.987654,
            "render.font_path": "/Users/example/private-font.ttf",
        },
        confirm=True,
    )
    snapshot = app.extensions["inktime_settings_repository"].snapshot(
        response.json["snapshot_id"]
    )
    assert "home_latitude" not in snapshot["before"]
    assert "home_latitude" not in snapshot["after"]
    assert all(item["old_value"] == {"status": "已設定"} for item in snapshot["items"])
    assert all(item["new_value"] == {"status": "已變更"} for item in snapshot["items"])

    exported = client.get("/api/v1/settings/export")
    assert exported.status_code == 200
    document = json.loads(exported.get_data(as_text=True))
    assert "home_latitude" not in document["settings"]
    assert document["sensitive_status"]["home_latitude"] == {"configured": True}
    assert "render.font_path" not in document["settings"]
    assert "/Users/example/private-font.ttf" not in exported.get_data(as_text=True)
    assert "webhook.bearer_token" not in exported.get_data(as_text=True)
    assert exported.headers["Cache-Control"] == "no-store"


def test_viewer_html_and_snapshot_apis_never_receive_sensitive_values(client, app):
    create_admin(app)
    login(client)
    exact_latitude = 24.987654
    exact_font_path = "/Users/example/private-font-unique.ttf"
    exact_webhook = "https://hooks.example.test/private/path?token=unique-token"
    changed = _post(
        client,
        "/api/v1/settings",
        {
            "home_latitude": exact_latitude,
            "render.font_path": exact_font_path,
            "notification.webhook_url": exact_webhook,
        },
        confirm=True,
    )
    assert changed.status_code == 200
    snapshot_id = changed.json["snapshot_id"]

    app.extensions["inktime_auth_repository"].create_user(
        "viewer-sensitive", "viewer-password-long", "viewer"
    )
    login(client, "viewer-sensitive", "viewer-password-long")
    settings_html = client.get("/settings").get_data(as_text=True)
    snapshot_body = client.get(
        f"/api/v1/settings/snapshots/{snapshot_id}"
    ).get_data(as_text=True)
    snapshot_list = client.get("/api/v1/settings/snapshots").get_data(as_text=True)
    metadata = client.get("/api/v1/settings/metadata").get_data(as_text=True)

    for secret in (
        str(exact_latitude),
        exact_font_path,
        exact_webhook,
        "unique-token",
        "127.0.0.1",
    ):
        assert secret not in settings_html
        assert secret not in snapshot_body
        assert secret not in snapshot_list
    assert str(exact_latitude) not in metadata
    assert exact_font_path not in metadata
    assert "source_ip" not in snapshot_body
    assert "source_ip" not in snapshot_list
    assert "已設定" in settings_html


def test_webhook_url_path_query_and_token_never_enter_snapshot_or_export(client, app):
    create_admin(app)
    login(client)
    webhook = "https://hooks.example.test/private/path?token=do-not-store"
    changed = _post(
        client,
        "/api/v1/settings",
        {"notification.webhook_url": webhook},
        confirm=True,
    )
    assert changed.status_code == 200
    snapshot_id = changed.json["snapshot_id"]
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            """
            SELECT before_json,after_json FROM settings_snapshots WHERE id=?
            """,
            (snapshot_id,),
        ).fetchone()
        item = connection.execute(
            """
            SELECT old_value_json,new_value_json
            FROM settings_snapshot_items WHERE snapshot_id=? AND key=?
            """,
            (snapshot_id, "notification.webhook_url"),
        ).fetchone()
    persisted_snapshot = " ".join(
        (row["before_json"], row["after_json"], item["old_value_json"], item["new_value_json"])
    )
    assert "/private/path" not in persisted_snapshot
    assert "do-not-store" not in persisted_snapshot
    exported = client.get("/api/v1/settings/export")
    assert exported.headers["Cache-Control"] == "no-store"
    assert "/private/path" not in exported.get_data(as_text=True)
    assert "do-not-store" not in exported.get_data(as_text=True)
    assert json.loads(exported.get_data(as_text=True))["sensitive_status"][
        "notification.webhook_url"
    ] == {"configured": True}


def test_rollback_preview_and_apply_create_new_snapshot(client, app):
    create_admin(app)
    login(client)
    changed = _post(
        client,
        "/api/v1/settings",
        {"analysis.concurrency": 2},
        confirm=True,
    )
    source_snapshot = changed.json["snapshot_id"]
    preview = _post(
        client,
        f"/api/v1/settings/snapshots/{source_snapshot}/rollback-preview",
        {},
    )
    assert preview.status_code == 200
    assert preview.json["updates"]["analysis.concurrency"] == 1
    applied = _post(
        client,
        f"/api/v1/settings/snapshots/{source_snapshot}/rollback",
        {"confirm": True},
    )
    assert applied.status_code == 200
    assert app.extensions["inktime_settings_repository"].get("analysis.concurrency") == 1
    source = next(
        row
        for row in app.extensions["inktime_settings_repository"].all()
        if row["key"] == "analysis.concurrency"
    )
    assert source["effective_source"] == "Default"
    assert source["stored_value"] is None
    snapshots = app.extensions["inktime_settings_repository"].snapshots()
    assert len(snapshots) == 2
    assert snapshots[0]["rollback_source_snapshot_id"] == source_snapshot


def test_rollback_preview_is_exact_changed_keys_diff_and_marks_later_overwrite(client, app):
    create_admin(app)
    login(client)
    source = _post(
        client,
        "/api/v1/settings",
        {"analysis.concurrency": 2},
        confirm=True,
    ).json["snapshot_id"]
    assert _post(
        client,
        "/api/v1/settings",
        {"general.timezone": "UTC"},
    ).status_code == 200
    assert _post(
        client,
        "/api/v1/settings",
        {"analysis.concurrency": 3},
        confirm=True,
    ).status_code == 200

    preview = _post(
        client,
        f"/api/v1/settings/snapshots/{source}/rollback-preview",
        {},
    )
    assert preview.status_code == 200
    assert preview.json["rollback_scope"] == "snapshot_changed_keys_only"
    assert preview.json["updates"] == {"analysis.concurrency": 1}
    assert preview.json["diff"] == [
        {
            "key": "analysis.concurrency",
            "label_zh_tw": "AI 分析並行數",
            "current_value": 3,
            "target_value": 1,
            "changed_since_snapshot": True,
        }
    ]
    assert preview.json["overwrites_changes_after_snapshot"] is True
    assert "general.timezone" not in preview.json["updates"]


def test_removed_legacy_snapshot_key_is_serialized_and_skipped_safely(client, app):
    create_admin(app)
    login(client)
    snapshot_id = "legacy-removed-setting"
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            INSERT INTO settings_snapshots(
                id,created_at,actor_id,source_ip,reason,before_json,after_json,
                changed_keys_json,schema_version,application_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                "2026-07-24T00:00:00+00:00",
                "legacy-admin",
                "192.0.2.55",
                "legacy",
                '{"removed.private_key":"old-value"}',
                '{"removed.private_key":"new-value"}',
                '["removed.private_key"]',
                0,
                "legacy",
            ),
        )
        connection.execute(
            """
            INSERT INTO settings_snapshot_items(
                snapshot_id,key,old_value_json,new_value_json,restored_default
            ) VALUES (?,?,?,?,0)
            """,
            (
                snapshot_id,
                "removed.private_key",
                '"old-value"',
                '"new-value"',
            ),
        )

    detail = client.get(f"/api/v1/settings/snapshots/{snapshot_id}")
    assert detail.status_code == 200
    assert detail.json["items"][0]["metadata"]["label_zh_tw"] == "已移除設定"
    assert detail.json["items"][0]["metadata"]["removed"] is True
    assert detail.json["items"][0]["old_value"] == {"status": "已移除設定"}
    assert "old-value" not in detail.get_data(as_text=True)

    preview = _post(
        client,
        f"/api/v1/settings/snapshots/{snapshot_id}/rollback-preview",
        {},
    )
    assert preview.status_code == 200
    assert preview.json["unknown_keys"] == ["removed.private_key"]
    assert preview.json["updates"] == {}
    applied = _post(
        client,
        f"/api/v1/settings/snapshots/{snapshot_id}/rollback",
        {"confirm": True},
    )
    assert applied.status_code == 200
    assert applied.json["updated"] == 0


def test_sensitive_snapshot_keys_require_manual_rollback(client, app):
    create_admin(app)
    login(client)
    source = _post(
        client,
        "/api/v1/settings",
        {
            "home_latitude": 23.456789,
            "notification.webhook_url": "https://example.test/hook?token=manual",
        },
        confirm=True,
    ).json["snapshot_id"]
    preview = _post(
        client,
        f"/api/v1/settings/snapshots/{source}/rollback-preview",
        {},
    )
    assert preview.status_code == 200
    assert preview.json["sensitive_unrestorable_keys"] == [
        "home_latitude",
        "notification.webhook_url",
    ]
    assert preview.json["updates"] == {}
    assert preview.json["changed_keys"] == []


def test_import_preview_has_no_side_effect_and_apply_skips_unknown_keys(client, app):
    create_admin(app)
    login(client)
    document = {
        "format": "inktime-settings",
        "version": 1,
        "settings": {
            "analysis.concurrency": 3,
            "home_latitude": 20.0,
            "future.unknown": True,
            "docker.port": 9999,
        },
    }
    preview = _post(client, "/api/v1/settings/import-preview", document)
    assert preview.status_code == 200
    assert preview.json["changes"] == {"analysis.concurrency": 3}
    assert preview.json["unknown_keys"] == ["future.unknown"]
    assert preview.json["blocked_keys"] == ["docker.port", "home_latitude"]
    assert "部署" in preview.json["blocked_reasons"]["docker.port"]
    assert app.extensions["inktime_settings_repository"].get("analysis.concurrency") == 1
    assert app.extensions["inktime_settings_repository"].snapshots() == []

    applied = _post(
        client,
        "/api/v1/settings/import",
        {"confirm": True, "document": document},
    )
    assert applied.status_code == 200
    assert app.extensions["inktime_settings_repository"].get("analysis.concurrency") == 3


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(client, app, invalid):
    create_admin(app)
    login(client)
    response = _post(
        client,
        "/api/v1/settings",
        {"analysis.stage_two_threshold": invalid},
        confirm=True,
    )
    assert response.status_code == 400
    assert "有限數字" in response.json["message"]


def test_fractional_integer_is_rejected_without_truncation(client, app):
    create_admin(app)
    login(client)
    response = _post(
        client,
        "/api/v1/settings",
        {"analysis.concurrency": 1.9},
        confirm=True,
    )
    assert response.status_code == 400
    assert app.extensions["inktime_settings_repository"].get("analysis.concurrency") == 1


def test_runtime_unwired_setting_is_read_only_for_api_import_and_ui(client, app):
    create_admin(app)
    login(client)
    direct = _post(
        client,
        "/api/v1/settings",
        {"observability.debug_level": "detailed"},
        confirm=True,
    )
    assert direct.status_code == 400
    assert "僅供唯讀" in direct.json["message"]

    document = {
        "format": "inktime-settings",
        "version": 1,
        "settings": {"observability.debug_level": "detailed"},
    }
    preview = _post(client, "/api/v1/settings/import-preview", document)
    assert preview.status_code == 200
    assert preview.json["changes"] == {}
    assert preview.json["blocked_keys"] == ["observability.debug_level"]
    assert "尚未接上 Runtime" in preview.json["blocked_reasons"][
        "observability.debug_level"
    ]

    body = client.get("/settings").get_data(as_text=True)
    assert 'data-key="observability.debug_level"' in body
    assert 'data-scope="not_wired"' in body
    assert "已儲存但尚未生效／尚未支援" in body
    assert 'name="observability.debug_level" disabled' in body


def test_device_override_and_effective_scope_metadata_use_actual_whitelists():
    allowed = {
        key
        for key, definition in SETTING_DEFINITIONS.items()
        if definition["device_override_allowed"]
    }
    assert allowed == DEVICE_OVERRIDE_KEYS
    assert SETTING_DEFINITIONS["render.layout"]["device_override_allowed"] is True
    assert SETTING_DEFINITIONS["render.dither"]["device_override_allowed"] is False
    assert SETTING_DEFINITIONS["render.layout"]["effective_scope"] == "next_render"
    assert (
        SETTING_DEFINITIONS["device.default_schedule"]["effective_scope"]
        == "future_device_only"
    )
    assert (
        SETTING_DEFINITIONS["observability.debug_level"]["effective_scope"]
        == "not_wired"
    )
    assert SETTING_DEFINITIONS["render.layout"]["existing_release_unchanged"] is True
    assert "建立新 Release" in SETTING_DEFINITIONS["render.layout"]["effective_note"]


def test_snapshot_retention_is_bounded_and_keeps_latest_rollback_source(app):
    repository = app.extensions["inktime_settings_repository"]
    first = repository.update_many(
        {"analysis.concurrency": 2},
        changed_by="test",
        source_ip="127.0.0.1",
    )["snapshot_id"]
    repository.rollback(
        first,
        changed_by="test",
        source_ip="127.0.0.1",
    )
    for index in range(105):
        repository.update_many(
            {"analysis.concurrency": 2 if index % 2 == 0 else 1},
            changed_by="test",
            source_ip="127.0.0.1",
        )
    snapshots = repository.snapshots(200)
    assert len(snapshots) == 100
    with app.extensions["inktime_database"].session() as connection:
        latest_rollback = connection.execute(
            """
            SELECT rollback_source_snapshot_id FROM settings_snapshots
            WHERE rollback_source_snapshot_id IS NOT NULL
            ORDER BY created_at DESC,id DESC LIMIT 1
            """
        ).fetchone()
        assert latest_rollback["rollback_source_snapshot_id"] == first
        assert connection.execute(
            "SELECT COUNT(*) FROM settings_snapshots WHERE id=?", (first,)
        ).fetchone()[0] == 1


def test_viewer_has_read_only_governed_ui(client, app):
    create_admin(app)
    app.extensions["inktime_auth_repository"].create_user(
        "viewer", "viewer-password-long", "viewer"
    )
    login(client, "viewer", "viewer-password-long")
    body = client.get("/settings").get_data(as_text=True)
    assert "設定控制中心" in body
    assert "每日 AI 分析照片上限" in body
    assert 'data-can-edit="false"' in body
    assert 'id="save-settings"' not in body
    assert "匯出安全設定" not in body
    assert _post(client, "/api/v1/settings", {"analysis.concurrency": 2}).status_code == 403


def test_ui_contains_dirty_search_filter_snapshot_and_accessibility_contracts(client, app):
    create_admin(app)
    login(client)
    body = client.get("/settings").get_data(as_text=True)
    for marker in (
        'id="settings-search"',
        'id="settings-category-filter"',
        'name="settings-mode"',
        'id="dirty-count"',
        "beforeunload",
        "Object.fromEntries(dirty)",
        'id="settings-preview-dialog"',
        'id="snapshot-dialog"',
        'id="import-dialog"',
        'role="alert"',
        "Rollback 實際 Diff",
        "敏感設定無法自動 Rollback",
        "只回復該 Snapshot 的 changed_keys",
        "既有 Release 不會改變",
    ):
        assert marker in body
    assert "完整裝置群組覆寫" not in body
    assert "改變 Cache Fingerprint" in body
    assert SETTING_DEFINITIONS["analysis.ai_daily_photo_limit"]["advanced"] is True
    assert SETTING_DEFINITIONS["analysis.ai_daily_photo_limit"]["risk"] == "high"
    assert SETTING_DEFINITIONS["analysis.caption_variants_enabled"]["dependencies"] == [
        {"key": "analysis.advanced_caption_enabled", "equals": True}
    ]
