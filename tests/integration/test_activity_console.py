from __future__ import annotations

from datetime import datetime, timezone

from inktime.app.api.operations import _activity_cursor, _timeline_rows
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
        connection.execute(
            "INSERT INTO job_events(job_id,event,message,details_json,created_at) VALUES ('activity-job','completed','工作完成','{}',?)",
            (now,),
        )
        connection.execute(
            "INSERT INTO device_events(device_id,level,event,message,details_json,created_at) VALUES (?,'warning','status_report','Bearer secret-token','{}',?)",
            (device_id, now),
        )
        connection.execute(
            "INSERT INTO job_errors(job_id,component,error_code,fingerprint,severity,message,first_seen_at,last_seen_at) VALUES ('activity-job','worker','WORKER-001','activity-error','error','/Users/test/private.jpg',?,?)",
            (now, now),
        )
    app.extensions["inktime_observability_service"].record(
        "INFO", "test", "activity", "已遮蔽 API key", job_id="activity-job"
    )


def test_activity_is_bounded_unifies_sources_and_redacts(client, app):
    create_admin(app)
    login(client)
    _seed_sources(app)
    response = client.get("/api/v1/activity?job_id=activity-job")
    assert response.status_code == 200
    assert len(response.json["events"]) <= 200
    assert {event["source"] for event in response.json["events"]} >= {"activity", "job_events", "job_errors"}
    assert "secret-token" not in str(response.json)
    first_cursor = response.json["next_cursor"]
    app.extensions["inktime_observability_service"].record("INFO", "test", "new_activity", "較新的事件")
    new_only = client.get(f"/api/v1/activity?job_id=activity-job&after={first_cursor}")
    assert new_only.status_code == 200
    assert all(event["source"] == "activity" for event in new_only.json["events"])
    page = client.get("/activity?job_id=activity-job")
    body = page.get_data(as_text=True)
    assert page.status_code == 200 and 'name="job_id" value="activity-job"' in body
    assert "MAX_ACTIVITY_EVENTS=200" in body
    assert "loadInFlight" in body
    assert "visibilitychange" in body
    assert "setInterval" not in body
    assert "cursor=''" in body
    assert "next_cursor" in body
    assert 'aria-label="顯示的警告等級"' in body
    assert 'data-severity="CRITICAL"' in body
    assert "技術詳細資料" in body
    assert "if(paused||document.hidden||!autoRefresh.checked)return" in body
    assert "if(paused){stopPoll();return;}" in body
    auto_refresh_start = body.index("if(!autoRefresh.checked){")
    auto_refresh_end = body.index("}", auto_refresh_start)
    auto_refresh_block = body[auto_refresh_start:auto_refresh_end]
    assert "stopPoll();" in auto_refresh_block
    assert "state.textContent='自動更新已關閉'" in auto_refresh_block
    assert "return;" in auto_refresh_block
    assert auto_refresh_block.index("stopPoll();") < auto_refresh_block.index("state.textContent")
    assert auto_refresh_block.index("state.textContent") < auto_refresh_block.index("return;")
    assert "function stopPoll(){if(pollTimer)clearTimeout(pollTimer);pollTimer=null;}" in body
    assert "loadInFlight=true" in body
    assert "if(newOnly){if(events.length)" in body
    assert "button.textContent=`有 ${pending.length} 筆新事件`;}return;}" in body


def test_activity_incremental_empty_page_keeps_the_rendered_timeline_contract(client, app):
    create_admin(app)
    login(client)
    _seed_sources(app)

    body = client.get("/activity?job_id=activity-job").get_data(as_text=True)

    # An empty incremental response means "no new events". It must return
    # before the full-load branch clears the already-rendered timeline.
    incremental = body.index("if(newOnly){if(events.length)")
    early_return = body.index("return;}summaryLast", incremental)
    clear_existing = body.index("list.innerHTML=''", early_return)
    assert incremental < early_return < clear_existing


def test_activity_filters_before_source_limit_so_older_matches_are_visible(client, app):
    create_admin(app)
    login(client)
    now = datetime.now(timezone.utc).isoformat()
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "INSERT INTO activity_events(source,source_id,severity,component,event,message,details_json,created_at) VALUES ('test','older-match','INFO','wanted','history','older matching event','{}',?)",
            (now,),
        )
        connection.executemany(
            "INSERT INTO activity_events(source,source_id,severity,component,event,message,details_json,created_at) VALUES ('test',?,'INFO','other','recent','nonmatch','{}',?)",
            [(f"recent-{index}", now) for index in range(60)],
        )

    response = client.get("/api/v1/activity?component=wanted")

    assert response.status_code == 200
    assert [event["source_id"] for event in response.json["events"]] == ["older-match"]


def test_activity_cursor_keeps_latest_initial_page_and_drains_each_source_without_gaps(app):
    _seed_sources(app)
    database = app.extensions["inktime_database"]
    now = datetime.now(timezone.utc).isoformat()
    filters = {"severity": "", "component": "", "job_id": "", "photo_id": "", "device_id": "", "query": ""}
    with database.session() as connection:
        device_id = str(connection.execute("SELECT id FROM devices LIMIT 1").fetchone()[0])
        connection.executemany(
            "INSERT INTO activity_events(source,source_id,severity,component,event,message,details_json,created_at) VALUES ('burst_activity',?,'INFO','test','history',?,'{}',?)",
            [(f"history-{index}", str(index), now) for index in range(60)],
        )
        connection.executemany(
            "INSERT INTO job_events(job_id,event,message,details_json,created_at) VALUES ('activity-job','history',?,'{}',?)",
            [(str(index), now) for index in range(60)],
        )
        connection.executemany(
            "INSERT INTO device_events(device_id,level,event,message,details_json,created_at) VALUES (?,'info','history',?,'{}',?)",
            [(device_id, str(index), now) for index in range(60)],
        )
        connection.executemany(
            "INSERT INTO job_errors(job_id,component,error_code,fingerprint,severity,message,first_seen_at,last_seen_at) VALUES ('activity-job','test','BURST-'||?,'burst-'||?,'warning',?,?,?)",
            [(str(index), str(index), str(index), now, now) for index in range(60)],
        )

    cursor = {"activity": 0, "job": 0, "device": 0, "error": 0}
    with database.session() as connection:
        initial, encoded = _timeline_rows(connection, filters, cursor)
    initial_cursor = _activity_cursor(encoded)
    assert len([item for item in initial if item["source"] == "burst_activity"]) == 50
    assert len([item for item in initial if item["source"] == "job_events"]) == 50
    assert len([item for item in initial if item["source"] == "device_events"]) == 50
    assert len([item for item in initial if item["source"] == "job_errors"]) == 50

    with database.session() as connection:
        connection.executemany(
            "INSERT INTO activity_events(source,source_id,severity,component,event,message,details_json,created_at) VALUES ('burst_activity',?,'INFO','test','new',?,'{}',?)",
            [(f"new-{index}", str(index), now) for index in range(120)],
        )
        connection.executemany(
            "INSERT INTO job_events(job_id,event,message,details_json,created_at) VALUES ('activity-job','new',?,'{}',?)",
            [(str(index), now) for index in range(120)],
        )
        connection.executemany(
            "INSERT INTO device_events(device_id,level,event,message,details_json,created_at) VALUES (?,'info','new',?,'{}',?)",
            [(device_id, str(index), now) for index in range(120)],
        )
        connection.executemany(
            "INSERT INTO job_errors(job_id,component,error_code,fingerprint,severity,message,first_seen_at,last_seen_at) VALUES ('activity-job','test','NEW-'||?,'new-'||?,'warning',?,?,?)",
            [(str(index), str(index), str(index), now, now) for index in range(120)],
        )

    seen = {"burst_activity": set(), "job_events": set(), "device_events": set(), "job_errors": set()}
    for _ in range(3):
        with database.session() as connection:
            page, encoded = _timeline_rows(connection, filters, initial_cursor)
        initial_cursor = _activity_cursor(encoded)
        for item in page:
            if item["source"] in seen:
                seen[item["source"]].add(str(item["source_id"]))
    assert {len(values) for values in seen.values()} == {120}


def test_activity_access_is_read_only_for_viewer(client, app):
    create_admin(app)
    app.extensions["inktime_auth_repository"].create_user("viewer", "viewer-passphrase", role="viewer")
    assert client.get("/activity").status_code in {302, 401}
    login(client, "viewer", "viewer-passphrase")
    assert client.get("/activity").status_code == 200
    assert client.get("/api/v1/activity/download").status_code == 403
    response = client.post(
        "/api/v1/settings", json={"observability.debug_enabled": True}, headers={"X-CSRF-Token": csrf(client)}
    )
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
        "DEBUG",
        "analysis",
        "caption_cache_hit",
        "Prompt Bearer secret-token",
        photo_id="photo",
        trace_id=fingerprint,
        api_key="not-for-activity",
    )
    body = client.get("/api/v1/activity?photo_id=photo").get_data(as_text=True)
    assert "secret-token" not in body and "not-for-activity" not in body
    page = client.get("/settings").get_data(as_text=True)
    assert "照片描述與相框文案" in page and "系統監控與除錯" in page
