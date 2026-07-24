from __future__ import annotations

from datetime import datetime, timezone

from tests.conftest import create_admin, csrf, login


def _seed_sources(app):
    now = datetime.now(timezone.utc).isoformat()
    devices = app.extensions["inktime_device_repository"]
    device_id, _token = devices.create("測試裝置")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "INSERT INTO jobs(id,kind,name,status,strategy,settings_json,created_at) VALUES ('activity-job','analysis','Activity','completed','local','{}',?)",
            (now,),
        )
        connection.execute("INSERT INTO job_events(job_id,event,message,details_json,created_at) VALUES ('activity-job','completed','工作完成','{}',?)", (now,))
        connection.execute("INSERT INTO device_events(device_id,level,event,message,details_json,created_at) VALUES (?,'warning','status_report','Bearer secret-token','{}',?)", (device_id, now))
        connection.execute("INSERT INTO job_errors(job_id,component,error_code,fingerprint,severity,message,first_seen_at,last_seen_at) VALUES ('activity-job','worker','WORKER-001','activity-error','error','/Users/test/private.jpg',?,?)", (now, now))
    app.extensions["inktime_observability_service"].record("INFO", "test", "activity", "已遮蔽 API key", job_id="activity-job")


def test_activity_is_bounded_unifies_sources_and_redacts(client, app):
    create_admin(app)
    login(client)
    _seed_sources(app)
    response = client.get("/api/v1/activity?job_id=activity-job")
    assert response.status_code == 200
    assert len(response.json["events"]) <= 200
    assert {event["source"] for event in response.json["events"]} >= {"activity", "job_events", "job_errors"}
    assert "secret-token" not in str(response.json)
    first_id = max(int(event["id"]) for event in response.json["events"] if event["source"] == "activity")
    app.extensions["inktime_observability_service"].record("INFO", "test", "new_activity", "較新的事件")
    new_only = client.get(f"/api/v1/activity?after={first_id}")
    assert new_only.status_code == 200
    assert all(event["source"] == "activity" for event in new_only.json["events"])
    page = client.get("/activity?job_id=activity-job")
    assert page.status_code == 200 and 'name="job_id" value="activity-job"' in page.get_data(as_text=True)


def test_activity_access_is_read_only_for_viewer(client, app):
    create_admin(app)
    app.extensions["inktime_auth_repository"].create_user("viewer", "viewer-passphrase", role="viewer")
    assert client.get("/activity").status_code in {302, 401}
    login(client, "viewer", "viewer-passphrase")
    assert client.get("/activity").status_code == 200
    assert client.get("/api/v1/activity/download").status_code == 403
    response = client.post("/api/v1/settings", json={"observability.debug_enabled": True}, headers={"X-CSRF-Token": csrf(client)})
    assert response.status_code == 403


def test_caption_and_observability_settings_coexist_and_caption_events_are_redacted(client, app):
    create_admin(app)
    login(client)
    settings = app.extensions["inktime_settings_repository"]
    analysis = app.extensions["inktime_analysis_service"]
    before = analysis._prompt_version(analysis._caption_controls())
    response = client.post(
        "/api/v1/settings",
        json={
            "analysis.caption_min_chars": 121,
            "analysis.advanced_caption_enabled": True,
            "observability.debug_enabled": True,
        },
        headers={
            "X-CSRF-Token": csrf(client),
            "X-InkTime-Confirm-Risk": "true",
        },
    )
    assert response.status_code == 200
    assert settings.get("analysis.caption_min_chars") == 121
    assert analysis._prompt_version(analysis._caption_controls()) != before
    fingerprint = analysis._prompt_version(analysis._caption_controls())
    settings.update("observability.stuck_job_minutes", 6, changed_by="test", source_ip="test")
    assert analysis._prompt_version(analysis._caption_controls()) == fingerprint
    app.extensions["inktime_observability_service"].record(
        "DEBUG", "analysis", "caption_cache_hit", "Prompt Bearer secret-token",
        photo_id="photo", trace_id=fingerprint, api_key="not-for-activity",
    )
    body = client.get("/api/v1/activity?photo_id=photo").get_data(as_text=True)
    assert "secret-token" not in body and "not-for-activity" not in body
    page = client.get("/settings").get_data(as_text=True)
    assert "照片描述與相框文案" in page and "系統監控與除錯" in page
