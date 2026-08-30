from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path

from PIL import Image

from inktime.app.repositories.settings import SETTING_DEFINITIONS
from inktime.app.workers.runner import WorkerRunner
from tests.conftest import create_admin, csrf, login
from tests.integration.test_jobs import add_photos
from tests.unit.test_analysis_schema import valid_result


def test_primary_management_pages_render(client, app):
    create_admin(app)
    login(client)
    for path in (
        "/dashboard",
        "/photos",
        "/review/photos",
        "/jobs",
        "/providers",
        "/analysis/batches",
        "/scoring",
        "/costs",
        "/simulator",
        "/virtual-display",
        "/rendering",
        "/devices",
        "/energy",
        "/maintenance",
        "/settings",
        "/help/controls",
        "/diagnostics",
        "/errors",
        "/backups",
        "/decision-traces",
        "/feedback",
        "/shadow",
        "/device-queues",
        "/retention",
        "/rollouts",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        body = response.get_data(as_text=True)
        assert "zh-Hant-TW" in body
        if path != "/virtual-display":
            assert 'class="page-guide"' in body
            assert "小提示與使用說明" in body

    virtual_display = client.get("/virtual-display").get_data(as_text=True)
    assert 'class="receiver-guide"' in virtual_display
    assert "虛擬墨水屏怎麼使用" in virtual_display

    simulator = client.get("/simulator").get_data(as_text=True)
    for expected_control in ("舊微雪算法", "新算法", "A/B 預覽", "傳送到墨水屏測試"):
        assert expected_control in simulator
    settings = client.get("/settings").get_data(as_text=True)
    assert "Good Display 原廠相容" in settings
    assert "照片平滑（減少色塊／雜點）" in settings

    rendering = client.get("/rendering").get_data(as_text=True)
    assert 'id="release-action-status"' in rendering
    assert "inktimeSetStatus('#release-action-status',d.message||'發布失敗'" in rendering
    assert "inktimeSetStatus('#release-action-status',d.message||'回滾失敗'" in rendering

    providers = client.get("/providers").get_data(as_text=True)
    assert 'id="provider-action-status"' in providers
    assert "alert(" not in providers

    jobs = client.get("/jobs").get_data(as_text=True)
    assert 'type="submit" class="primary">確認建立' in jobs
    assert "limit:'nullable-integer'" in jobs
    assert "budget:'float'" in jobs
    assert "if(body.limit===null)delete body.limit;" in jobs


def test_login_replaces_page_guide_with_safe_version_and_status_summary(client, app):
    create_admin(app)

    body = client.get("/login").get_data(as_text=True)

    assert "登入提示" not in body
    assert 'class="page-guide"' not in body
    assert "目前服務狀態" in body
    assert "InkTime 版本" in body
    assert "部署版本" in body
    assert "執行環境" in body
    assert "資料庫" in body


def test_dashboard_renders_one_page_setup_overview_with_direct_actions(client, app):
    create_admin(app)
    login(client)

    body = client.get("/dashboard").get_data(as_text=True)

    assert 'id="setup-overview"' in body
    assert "完整設定導覽" in body
    assert 'role="progressbar"' in body
    assert "第 1 級" in body
    assert 'href="#setup-overview"' in body
    assert "狀態等級由低到高：正常、提示、警告、錯誤、嚴重" not in body
    for title in (
        "照片來源與掃描",
        "分析方式",
        "Vision Provider 與模型",
        "用量限制與預算保護",
        "電子紙裝置",
        "自動排程",
        "備份與保留",
    ):
        assert title in body
    for action_url in (
        "/photos",
        "/settings?search=分析執行模式",
        "/providers",
        "/settings?search=預算",
        "/devices",
        "/schedules",
        "/backups",
    ):
        assert f'href="{action_url}"' in body


def test_control_glossary_lists_every_setting_options_actions_and_jumps(client, app):
    create_admin(app)
    login(client)

    body = client.get("/help/controls").get_data(as_text=True)

    assert "功能與設定說明大全" in body
    assert 'id="glossary-search"' in body
    assert 'id="glossary-kind-filter"' in body
    assert "automatic_ai" in body
    assert "自動 AI 分析" in body
    assert "送入 AI／批次送入 AI" in body
    assert "操作後的影響" in body
    assert "秘密值只顯示設定狀態" in body
    for key in SETTING_DEFINITIONS:
        assert f'id="setting-{key.replace(".", "-")}"' in body

    settings = client.get("/settings").get_data(as_text=True)
    assert 'href="/help/controls"' in settings
    assert 'id="setting-analysis-execution_mode"' in settings
    assert 'href="/help/controls#setting-analysis-execution_mode"' in settings

def test_shared_confirmation_dialog_resets_and_normalizes_cancel_state(client, app):
    create_admin(app)
    login(client)

    body = client.get("/dashboard").get_data(as_text=True)

    reset = body.index("dialog.returnValue = '';")
    cancel_listener = body.index("dialog.addEventListener('cancel', cancelled);")
    shown = body.index("dialog.showModal();", cancel_listener)
    assert reset < cancel_listener < shown
    assert "const cancelled = () => { dialog.returnValue = 'cancel'; };" in body
    assert "dialog.removeEventListener('cancel', cancelled);" in body
    assert "dialog.returnValue === 'confirm'" in body

def test_shared_preview_retry_and_typed_job_contracts_are_rendered(client, app):
    create_admin(app)
    login(client)

    base = client.get("/dashboard").get_data(as_text=True)
    jobs = client.get("/jobs").get_data(as_text=True)
    rendering = client.get("/rendering").get_data(as_text=True)

    assert "window.inktimeFetchPreview" in base
    assert "response.headers.get('Retry-After')" in base
    assert "預覽請求過於頻繁，請稍後再試" in base
    assert "signal?.addEventListener('abort', cancel" in base
    assert "if(body.limit===null)delete body.limit" in jobs
    assert "pending_total??0" in jobs
    assert "limited_to??0" in jobs
    assert "目前分析執行模式是「僅使用本機選片」" in jobs
    assert '/settings?search=分析執行模式' in jobs
    assert "目前沒有已啟用且設定完整的 Vision Provider" not in jobs
    assert "AI 尚未完成啟用" in jobs
    assert "必要項目 1 / 3" in jobs
    assert "完成下列全部必要項目後" in jobs
    assert "靜態設定就緒" in jobs

    providers = client.get("/providers").get_data(as_text=True)
    assert "AI 尚未完成啟用" in providers
    assert "實際連線驗收不等於靜態設定就緒" in providers
    assert "window.inktimeFetchPreview(statusUrl" in rendering
    assert "window.inktimeFetchPreview(result.preview_url" in rendering

def test_job_detail_live_status_exposes_items_and_only_valid_actions(client, app):
    administrator_id = create_admin(app)
    login(client)
    repository = app.extensions["inktime_job_repository"]
    job_id = repository.create_maintenance(
        kind="scan",
        name="即時狀態測試",
        settings={"root_path": "/photos"},
        created_by=administrator_id,
    )
    item_id = str(repository.list_items(job_id)[0]["id"])

    page = client.get(f"/jobs/{job_id}")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert 'id="job-items"' in body
    assert "狀態每 3 秒自動更新" in body
    assert item_id in body
    assert 'data-action="start"' in body
    assert 'data-action="pause" disabled' in body

    status = client.get(f"/api/v1/jobs/{job_id}")
    assert status.status_code == 200
    assert status.json["available_actions"] == ["start", "cancel"]
    assert status.json["items"] == [
        {
            "id": item_id,
            "photo_id": None,
            "status": "pending",
            "stage": "queued",
            "attempts": 0,
            "error_code": None,
        }
    ]


def test_retry_failed_control_restarts_the_job_in_one_action(client, app):
    administrator_id = create_admin(app)
    login(client)
    repository = app.extensions["inktime_job_repository"]
    job_id = repository.create_maintenance(
        kind="scan",
        name="重跑測試",
        settings={"root_path": "/photos"},
        created_by=administrator_id,
    )
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE jobs SET status='completed_with_errors',failed_items=1 WHERE id=?",
            (job_id,),
        )
        connection.execute(
            "UPDATE job_items SET status='failed',error_code='SCAN-TEST' WHERE job_id=?",
            (job_id,),
        )

    response = client.post(
        f"/api/v1/jobs/{job_id}/retry-failed",
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 200
    assert response.json["affected"] == 1
    assert repository.get(job_id)["status"] == "running"
    assert repository.list_items(job_id)[0]["status"] == "pending"


def test_simulator_superseded_compare_aborts_fetch_and_poll_delay(client, app):
    create_admin(app)
    login(client)
    body = client.get("/simulator").get_data(as_text=True)

    assert "let compareController = null" in body
    assert "compareController.abort()" in body
    assert "waitForJob(created,{signal:controller.signal})" in body
    assert "window.inktimeFetchPreview(created.status_url,{signal})" in body
    assert "await abortableDelay(750,signal)" in body
    assert "clearTimeout(timer);reject(abortError())" in body
    assert "error?.name!=='AbortError'" in body
    assert "if(compareController===controller)" in body
    assert "pendingCompare" not in body


def test_device_runtime_summary_exposes_persisted_versions_and_unknowns_as_null(client, app):
    create_admin(app)
    login(client)
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "Runtime Summary",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00", "20:00"],
        sync_strategy="fixed_daily",
        sync_time="07:30",
    )
    response = client.get(f"/api/v1/devices/{device_id}/runtime-summary")
    assert response.status_code == 200
    body = response.get_json()
    assert body["desired_config_version"] == 1
    assert body["applied_config_version"] == 0
    assert body["desired_offline_schedule_version"] == 1
    assert body["applied_offline_schedule_version"] == 0
    assert body["today_timeline"] == []
    assert body["next_display_slot"] is None
    assert body["active_schedule"] is None
    assert body["staged_schedule"] is None
    assert body["last_known"]["firmware_version"] is None
    assert body["fallback_recovery"] is None


def test_device_runtime_summary_polling_aborts_a_hung_request(client, app):
    create_admin(app)
    login(client)
    response = client.get("/devices")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "const requestTimeout=setTimeout(()=>controller.abort(),Math.max(0,deadline-Date.now()))" in body
    assert "clearTimeout(requestTimeout)" in body
    assert "runtimeSummaryControllers.forEach(controller=>controller.abort())" in body


def test_stock_photopainter_display_controls_are_scoped_and_server_side(client, app):
    create_admin(app)
    login(client)
    repository = app.extensions["inktime_device_repository"]
    hosted_id, _ = repository.create(
        "客廳相框",
        panel_profile="gdep073e01_6c",
        delivery_mode="stock_compat",
        stock_endpoint_host="10.23.45.67",
    )
    missing_host_id, _ = repository.create(
        "書房相框",
        panel_profile="safe_4c",
        delivery_mode="stock_compat",
    )
    legacy_id, _ = repository.create(
        "舊版相框",
        panel_profile="safe_4c",
        delivery_mode="legacy_online",
        stock_endpoint_host="192.168.1.51",
    )
    offline_id, _ = repository.create(
        "離線相框",
        panel_profile="safe_4c",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )

    page = client.get("/devices")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert body.count('class="primary stock-display"') == 2
    assert f'data-stock-display-device="{hosted_id}"' in body
    assert f'data-stock-display-device="{missing_host_id}"' in body
    assert f'data-stock-display-device="{legacy_id}"' not in body
    assert f'data-stock-display-device="{offline_id}"' not in body
    assert "支援 24-slot 裝置最多 24 個" in body
    assert "Legacy／未知裝置最多 12 個" in body
    assert "Stock Host：10.23.45.67" in body
    assert 'data-stock-host="10.23.45.67"' in body
    assert 'placeholder="192.168.1.50"' in body
    assert 'id="device-dialog"' in body
    assert 'id="token-dialog"' in body
    assert "Stock Host：未設定" in body
    assert 'disabled title="尚未設定 Stock LAN Host"' in body
    assert "尚未設定 Stock LAN Host" in body

    for expected in (
        "/api/v1/virtual-display/manifest?profile=",
        "/api/v1/devices/",
        "/stock-photopainter/display",
        "window.inktimeFetch",
        "最新 Release Manifest 不完整，已停止傳送。",
        "PhotoPainter 已接受圖片。請查看實體相框確認刷新結果。",
        "傳送中…",
        "dataset.pending",
    ):
        assert expected in body
    assert "/dataUP" not in body

    app.extensions["inktime_auth_repository"].create_user(
        "stock-viewer", "stock-viewer-password", "viewer"
    )
    viewer = app.test_client()
    login(viewer, "stock-viewer", "stock-viewer-password")
    viewer_body = viewer.get("/devices").get_data(as_text=True)
    assert "立即顯示最新 Release" not in viewer_body
    assert "data-stock-display-device" not in viewer_body
    assert "10.23.45.67" not in viewer_body
    assert "stock-display" not in viewer_body
    assert 'data-stock-host="10.23.45.67"' not in viewer_body
    assert "/stock-photopainter/display" not in viewer_body
    assert 'placeholder="192.168.1.50"' not in viewer_body
    assert 'id="device-dialog"' not in viewer_body
    assert 'id="token-dialog"' not in viewer_body
    assert "Stock PhotoPainter 模式" in viewer_body


def test_device_management_preserves_legacy_automatic_and_stock_controls(client, app):
    create_admin(app)
    login(client)
    repository = app.extensions["inktime_device_repository"]
    legacy_id, _ = repository.create("Legacy 相框", auth_mode="legacy_token")
    automatic_id, _ = repository.create("自製相框", auth_mode="automatic")
    stock_id, _ = repository.create(
        "Stock 相框",
        delivery_mode="stock_compat",
        stock_endpoint_host="10.23.45.67",
    )

    body = client.get("/devices").get_data(as_text=True)
    assert f'class="secondary regenerate" data-id="{legacy_id}"' in body
    assert f'class="secondary regenerate" data-id="{automatic_id}"' not in body
    assert f'class="secondary regenerate" data-id="{stock_id}"' not in body
    assert f'class="secondary repair-device" data-id="{automatic_id}"' in body
    assert f'data-stock-display-device="{stock_id}"' in body
    assert 'id="device-dialog"' in body
    assert 'id="token-dialog"' in body

    app.extensions["inktime_auth_repository"].create_user(
        "device-viewer", "device-viewer-password", "viewer"
    )
    viewer = app.test_client()
    login(viewer, "device-viewer", "device-viewer-password")
    viewer_body = viewer.get("/devices").get_data(as_text=True)
    assert "10.23.45.67" not in viewer_body
    assert "Stock Host：" not in viewer_body
    assert "stock-display" not in viewer_body
    assert "data-stock-display-device" not in viewer_body
    assert "data-stock-host" not in viewer_body
    assert "class=\"secondary regenerate\"" not in viewer_body
    assert "class=\"secondary repair-device\"" not in viewer_body
    assert 'id="device-dialog"' not in viewer_body
    assert 'id="token-dialog"' not in viewer_body
    assert "Stock PhotoPainter 模式" in viewer_body


def test_device_management_shows_offline_capability_without_secret_or_viewer_controls(client, app):
    create_admin(app)
    login(client)
    repository = app.extensions["inktime_device_repository"]
    repository.create("未知能力相框")
    repository.create(
        "確認 24-slot 相框",
        delivery_mode="inktime_offline_schedule",
        schedule_times=[f"{hour:02d}:00" for hour in range(24)],
        offline_schedule_max_slots=24,
    )
    ambiguous_id, ambiguous_token = repository.create(
        "隔離中的舊相框",
        delivery_mode="inktime_offline_schedule",
        schedule_times=[f"{hour:02d}:00" for hour in range(13)],
        offline_schedule_max_slots=24,
    )
    malformed_id, malformed_token = repository.create(
        "格式損壞的舊相框",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
        offline_schedule_max_slots=24,
    )
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            """
            UPDATE devices
            SET offline_schedule_max_slots=12,
                offline_schedule_capability_state='legacy_ambiguous',
                next_offline_prepare_at=NULL
            WHERE id=?
            """,
            (ambiguous_id,),
        )
        connection.execute(
            """
            UPDATE devices
            SET offline_schedule_max_slots=12,
                offline_schedule_capability_state='legacy_ambiguous',
                schedule_times_json='["08:00",',
                offline_schedule_json='{"legacy":"08:00"}',
                next_offline_prepare_at=NULL
            WHERE id=?
            """,
            (malformed_id,),
        )

    body = client.get("/devices").get_data(as_text=True)
    assert "尚未回報離線排程能力（保守上限 12 個時段）" in body
    assert "已確認支援 24 個離線時段（目前上限 24）" in body
    assert "離線排程能力尚未確認（保守上限 12 個時段）" in body
    assert "請重新配對或執行修復，以確認裝置能力。" in body
    assert ambiguous_token not in body
    assert malformed_token not in body
    assert 'data-offline-capability-state="legacy_ambiguous"' in body
    assert "deviceForm.dataset.scheduleSourceValid='false'" in body
    assert "const safeQuarantinedRemediation=" in body
    assert "if(!safeQuarantinedRemediation||deviceForm.dataset.scheduleSourceValid==='true'||scheduleEdited)" in body

    app.extensions["inktime_auth_repository"].create_user(
        "capability-viewer", "capability-viewer-password", "viewer"
    )
    viewer = app.test_client()
    login(viewer, "capability-viewer", "capability-viewer-password")
    viewer_body = viewer.get("/devices").get_data(as_text=True)
    assert "離線能力：" not in viewer_body
    assert "legacy_ambiguous" not in viewer_body
    assert "舊資料能力不明，已隔離" not in viewer_body
    assert ambiguous_token not in viewer_body
    assert malformed_token not in viewer_body
    assert "data-offline-capability-state" not in viewer_body
    assert 'id="device-dialog"' not in viewer_body


def test_batch_management_api_is_admin_only_and_strict_json(client, app):
    create_admin(app)
    login(client)
    assert client.get("/analysis/batches").status_code == 200
    assert client.post("/api/v1/analysis/batches/estimate", json={}).status_code == 403
    unknown = client.post(
        "/api/v1/analysis/batches/estimate",
        json={"scope": "sample", "unexpected": True},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert unknown.status_code == 400
    scalar = client.post(
        "/api/v1/analysis/batches/estimate",
        json=["sample"],
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert scalar.status_code == 400

    app.extensions["inktime_auth_repository"].create_user("batch-viewer", "batch-viewer-password", "viewer")
    viewer = app.test_client()
    login(viewer, "batch-viewer", "batch-viewer-password")
    assert viewer.get("/analysis/batches").status_code == 200
    assert viewer.post("/api/v1/analysis/batches/estimate", json={}).status_code == 403


def test_batch_management_api_dispatches_lifecycle_actions(client, app, monkeypatch):
    create_admin(app)
    login(client)

    detail = {
        "id": "batch-ui",
        "job_id": None,
        "scope": "sample",
        "status": "completed",
        "model": "gpt-5.6-luna",
        "total_items": 1,
        "imported_items": 1,
        "failed_items": 0,
        "missing_items": 0,
        "stale_items": 0,
        "input_tokens": 10,
        "cached_tokens": 0,
        "output_tokens": 2,
        "reasoning_tokens": 0,
        "actual_cost": 0.01,
        "average_cost": 0.01,
        "per_thousand_cost": 10.0,
        "eligible_missing_count": 1,
        "full_library_estimated_cost": 0.01,
        "schema_success_rate": 100.0,
        "actual_jsonl_bytes": 10,
        "cleanup_status": "completed",
        "peak_rss_bytes": 100,
        "candidate_snapshot_json": "[]",
        "shard_sizes": [],
        "items": [],
    }

    class FakeBatchService:
        def estimate(self, **kwargs):
            return {"candidate_count": 1, **kwargs}

        def submit(self, **kwargs):
            return {"batch_ids": ["batch-ui"], **kwargs}

        def get_detail(self, _batch_id):
            return detail

        def cancel(self, batch_id):
            return {"batch_id": batch_id, "status": "cancelled"}

        def retry_failed(self, batch_id, **kwargs):
            return {"batch_id": batch_id, "retry": True, **kwargs}

        def retry_cleanup(self, batch_id):
            return {"status": "cleanup_pending", "job_id": f"cleanup-{batch_id}"}

        def recover_submission(self, batch_id, remote_batch_id):
            return {"batch_id": batch_id, "remote_batch_id": remote_batch_id, "status": "validating"}

    monkeypatch.setitem(app.extensions, "inktime_batch_analysis_service", FakeBatchService())
    headers = {"X-CSRF-Token": csrf(client)}
    estimate = client.post(
        "/api/v1/analysis/batches/estimate",
        json={"scope": "sample", "budget_limit": 12.5},
        headers=headers,
    )
    assert estimate.status_code == 200
    assert "budget_limit" not in estimate.get_json()
    created = client.post("/api/v1/analysis/batches", json={"scope": "sample"}, headers=headers)
    assert created.status_code == 201
    assert client.get("/api/v1/analysis/batches").status_code == 200
    assert client.get("/api/v1/analysis/batches/batch-ui").status_code == 200
    assert client.get("/analysis/batches/batch-ui").status_code == 200
    assert (
        client.post("/api/v1/analysis/batches/batch-ui/cancel", json={}, headers=headers).status_code == 200
    )
    assert (
        client.post("/api/v1/analysis/batches/batch-ui/retry-failed", json={}, headers=headers).status_code
        == 200
    )
    cleanup = client.post("/api/v1/analysis/batches/batch-ui/retry-cleanup", json={}, headers=headers)
    assert cleanup.status_code == 200
    assert cleanup.json["job_id"] == "cleanup-batch-ui"
    recovered = client.post(
        "/api/v1/analysis/batches/batch-ui/recover-submission",
        json={"remote_batch_id": "batch-existing"},
        headers=headers,
    )
    assert recovered.status_code == 200
    assert recovered.json["remote_batch_id"] == "batch-existing"
    assert (
        client.post(
            "/api/v1/analysis/batches/batch-ui/recover-submission",
            json={"remote_batch_id": "batch-existing", "unexpected": True},
            headers=headers,
        ).status_code
        == 400
    )


def test_device_energy_dashboard_uses_automatic_telemetry_only(client, app):
    create_admin(app)
    login(client)
    repository = app.extensions["inktime_device_repository"]
    device_id, token = repository.create("客廳 PhotoPainter", panel_profile="gdep073e01_6c")
    status = client.post(
        "/api/device/v1/status",
        json={
            "firmware_version": "2.4.0",
            "battery_percent": 82,
            "battery_percent_estimated": True,
            "battery_voltage": 4.08,
            "usb_power": False,
            "display_updated": True,
            "last_refresh_duration_ms": 25_000,
            "wake_duration_ms": 61_000,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status.status_code == 200

    removed_profile = client.patch(
        f"/api/v1/devices/{device_id}/energy-profile",
        json={"standby_current_ma": 0.12},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert removed_profile.status_code == 404

    page = client.get(f"/energy?device_id={device_id}&days=30")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "裝置能源儀表板" in body
    assert "82.0%" in body
    assert "25.0 秒" in body
    assert "61.0 秒" in body
    assert "能源模型參數" not in body
    assert "待機電流" not in body

    api = client.get(f"/api/v1/devices/{device_id}/energy?days=30")
    assert api.status_code == 200
    assert api.json["summary"]["wake"]["average_seconds"] == 61.0
    assert api.json["summary"]["sample_count"] == 1
    assert "modeled" not in api.json["summary"]
    assert "standby_current_ma" not in api.json["device"]
    assert "token_hash" not in api.json["device"]


def test_theme_toggle_is_available_before_and_after_login(client, app):
    setup_page = client.get("/setup").get_data(as_text=True)
    assert 'id="theme-toggle"' in setup_page
    assert "inktime-theme" in setup_page

    create_admin(app)
    login(client)
    dashboard = client.get("/dashboard").get_data(as_text=True)
    assert 'id="theme-toggle"' in dashboard
    assert "深色模式" in dashboard


def test_scoring_rules_and_weights_create_a_new_version(client, app):
    create_admin(app)
    login(client)
    page = client.get("/scoring").get_data(as_text=True)
    assert 'textarea name="rules"' in page
    assert "人物互動或合照，大幅提高評分" in page

    current = app.extensions["inktime_scoring_repository"].current()
    custom_rules = str(current["rules"]) + "\n- 家庭合照再額外提高回憶價值。"
    response = client.post(
        "/api/v1/scoring/profiles",
        json={
            "name": "家庭照片優先",
            "rules": custom_rules,
            "memory_weight": 55,
            "beauty_weight": 15,
            "technical_weight": 10,
            "emotion_weight": 20,
            "favorite_bonus": 8,
        },
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 201
    assert app.extensions["inktime_settings_repository"].get("analysis.scoring_rules") == custom_rules
    assert app.extensions["inktime_scoring_repository"].current()["name"] == "家庭照片優先"


def test_scoring_test_upload_is_normalized_and_not_persisted(client, app, monkeypatch):
    create_admin(app)
    login(client)
    observed = {}

    def fake_analyze(path):
        observed["exists_during_analysis"] = path.exists()
        return {"ranking_score": 88, "analysis": {"caption": "測試照片"}}

    monkeypatch.setattr(app.extensions["inktime_scoring_lab_service"], "analyze", fake_analyze)
    image = BytesIO()
    Image.new("RGB", (32, 32), "navy").save(image, "JPEG")
    image.seek(0)
    response = client.post(
        "/api/v1/scoring/test",
        data={"photo": (image, "sample.jpg")},
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.json["ranking_score"] == 88
    assert observed["exists_during_analysis"] is True


def test_epaper_simulator_works_without_photo_database_or_model(client, app):
    create_admin(app)
    login(client)
    assert app.extensions["inktime_provider_repository"].list() == []
    image = BytesIO()
    Image.new("RGB", (32, 48), (42, 110, 180)).save(image, "PNG")
    image.seek(0)

    response = client.post(
        "/api/v1/rendering/simulate",
        data={
            "photo": (image, "standalone.png"),
            "profile": "safe_4c",
            "dither": "none",
            "fit": "contain",
            "strength": "0",
            "color_distance": "oklab",
        },
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )

    assert response.status_code == 202
    created = response.get_json()
    WorkerRunner(app).run_once()
    status = client.get(created["status_url"])
    assert status.status_code == 200
    result = status.get_json()["result"]
    assert result["payload_bytes"] == 96000
    preview = client.get(result["preview"])
    assert preview.status_code == 200
    rendered = Image.open(BytesIO(preview.data))
    assert rendered.size == (480, 800)
    assert set(rendered.get_flattened_data()).issubset(
        {(0, 0, 0), (255, 255, 255), (220, 30, 30), (245, 190, 25)}
    )
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0


def test_epaper_simulator_rejects_unknown_profile(client, app):
    create_admin(app)
    login(client)
    image = BytesIO()
    Image.new("RGB", (8, 8), "white").save(image, "PNG")
    image.seek(0)
    response = client.post(
        "/api/v1/rendering/simulate",
        data={"photo": (image, "sample.png"), "profile": "not-a-panel"},
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert response.json["error_code"] == "RENDER-004"


def test_virtual_display_receives_and_verifies_formal_release_payload(client, app):
    create_admin(app)
    login(client)
    manifest = app.extensions["inktime_release_publisher"].publish(
        [
            ("virtual-photo-1", Image.new("RGB", (480, 800), "gold")),
            ("virtual-photo-2", Image.new("RGB", (480, 800), "navy")),
        ],
        profile_key="safe_4c",
        dither="none",
        color_distance="rgb",
        dither_strength=0,
    )

    page = client.get("/virtual-display")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "RECEIVE ONLY" in body
    assert "不觸發發布" in body
    assert 'id="previous-frame"' in body
    assert 'id="next-frame"' in body
    assert "抖動算法" in body
    assert 'type="file"' not in body

    response = client.get("/api/v1/virtual-display/manifest?profile=safe_4c")
    assert response.status_code == 200
    assert response.headers["X-InkTime-Receiver"] == "virtual-display"
    assert response.json["release_id"] == manifest["release_id"]
    assert response.json["receiver"]["mode"] == "read_only"
    assert len(response.json["files"]) == 2
    file_entry = response.json["files"][0]
    payload = client.get(response.json["download_base_url"] + file_entry["name"])
    assert payload.status_code == 200
    assert payload.mimetype == "application/octet-stream"
    assert len(payload.data) == 96_000
    assert payload.headers["X-InkTime-Payload-SHA256"] == file_entry["sha256"]
    second_entry = response.json["files"][1]
    second_payload = client.get(response.json["download_base_url"] + second_entry["name"])
    assert second_payload.status_code == 200
    assert second_payload.headers["X-InkTime-Payload-SHA256"] == second_entry["sha256"]

    missing = client.get(f"/api/v1/virtual-display/releases/{manifest['release_id']}/files/manifest.json")
    assert missing.status_code == 404


def test_builtin_traditional_chinese_fonts_preview_and_switch(client, app):
    create_admin(app)
    login(client)
    settings = app.extensions["inktime_settings_repository"]
    assert settings.get("render.font_path") == "builtin:iansui"

    page = client.get("/rendering")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "芫荽 Iansui" in body
    assert "霞鶩文楷 TC" in body
    assert "手寫風格" in body
    assert "文青風格" in body
    assert "不會靜默改用" in body
    assert "2 個字型" in client.get("/diagnostics").get_data(as_text=True)

    preview = client.get("/api/v1/fonts/preview?reference=builtin%3Aiansui")
    assert preview.status_code == 200
    assert preview.mimetype == "image/png"
    assert Image.open(BytesIO(preview.data)).size == (760, 116)

    switched = client.post(
        "/api/v1/fonts/select",
        json={"reference": "builtin:lxgw-wenkai-tc"},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert switched.status_code == 200
    assert switched.json["status"] == "active"
    assert settings.get("render.font_path") == "builtin:lxgw-wenkai-tc"


def test_invalid_uploaded_font_never_replaces_current_font(client, app):
    create_admin(app)
    login(client)
    response = client.post(
        "/api/v1/fonts",
        data={"font": (BytesIO(b"not a real font"), "broken.ttf")},
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )

    assert response.status_code == 422
    assert response.json["error_code"] == "IMG-002"
    assert app.extensions["inktime_settings_repository"].get("render.font_path") == "builtin:iansui"
    assert not (app.extensions["inktime_font_manager"].root / "broken.ttf").exists()


def test_backup_is_integrity_checked_and_downloadable(client, app):
    create_admin(app)
    login(client)
    service = app.extensions["inktime_backup_service"]
    archive = service.create()
    manifest = service.validate(archive)
    assert "原始照片" in manifest["excludes"]
    response = client.get(f"/api/v1/backups/{archive.name}")
    assert response.status_code == 200
    assert response.mimetype == "application/zip"


def test_diagnostic_bundle_excludes_sensitive_categories(client, app):
    create_admin(app)
    login(client)
    response = client.get("/api/v1/diagnostics/bundle")
    assert response.status_code == 200
    body = response.get_data()
    for forbidden in (b"api_key", b"cookie", b"gps_lat", b"session.key"):
        assert forbidden not in body.lower()


def test_photo_manual_edit_is_audited(client, app):
    create_admin(app)
    login(client)
    photo_id = add_photos(app, 1)[0]
    response = client.patch(
        f"/api/v1/photos/{photo_id}",
        json={
            "favorite": True,
            "captured_at": "2026-07-17T10:00:00",
            "types": ["家庭"],
            "side_caption": "值得收藏的一天",
        },
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 200
    with app.extensions["inktime_database"].session() as connection:
        photo = connection.execute(
            "SELECT favorite,captured_at FROM photos WHERE id=?", (photo_id,)
        ).fetchone()
        event = connection.execute("SELECT event FROM photo_events WHERE photo_id=?", (photo_id,)).fetchone()
    assert tuple(photo) == (1, "2026-07-17T10:00:00")
    assert event["event"] == "manual_update"


def test_photo_console_shows_prefilter_metrics_model_text_and_generated_caption(client, app):
    create_admin(app)
    login(client)
    photo_id = add_photos(app, 1)[0]
    result = valid_result()
    app.extensions["inktime_photo_repository"].save_analysis(
        photo_id,
        None,
        "stage_one",
        "測試 Provider",
        "vision-model",
        result,
        '{"caption":"家人在公園散步。"}',
    )

    detail = client.get(f"/photos/{photo_id}")
    body = detail.get_data(as_text=True)
    assert detail.status_code == 200
    assert 'class="photo-overview-layout"' in body
    assert 'class="photo-analysis-grid"' in body
    assert 'class="photo-runtime-grid"' in body
    assert 'class="panel photo-history-panel"' in body
    assert 'class="photo-history-list"' in body
    assert "本機預篩選判斷" in body
    assert 'class="panel photo-prefilter-panel"' in body
    assert 'class="prefilter-rule-grid"' in body
    assert "檢查模式" in body
    assert "最終判斷" in body
    assert "疑似截圖（多重信號）" in body
    assert "目前門檻" in body
    assert "模糊分數" in body
    assert "過曝占比" in body
    assert "模型判斷文字結果" in body
    assert "家人在公園散步。" in body
    assert "產生的一句話（電子紙短文案）" in body
    assert "風把這一天留得很輕。" in body
    assert "測試 Provider / vision-model" in body
    assert body.index('class="photo-overview-layout"') < body.index('class="photo-analysis-grid"')
    assert body.index('class="photo-analysis-grid"') < body.index('class="panel photo-prefilter-panel"')
    assert body.index('class="panel photo-prefilter-panel"') < body.index('class="photo-runtime-grid"')

    listing = client.get("/photos").get_data(as_text=True)
    assert "家人在公園散步。" in listing
    assert "風把這一天留得很輕。" in listing


def test_photo_cards_show_total_score_and_e6_estimate(client, app):
    create_admin(app)
    login(client)
    analyzed_id, estimated_id = add_photos(app, 2)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET e6_score=100 WHERE id=?", (analyzed_id,))
        connection.execute("UPDATE photos SET e6_score=91.9 WHERE id=?", (estimated_id,))
    app.extensions["inktime_photo_repository"].save_analysis(
        analyzed_id,
        None,
        "stage_one",
        "測試 Provider",
        "vision-model",
        valid_result(),
        "{}",
        ranking_score=80,
    )

    body = client.get("/photos").get_data(as_text=True)

    assert "選片分 84.0（模型＋E6）" in body
    assert "為什麼兩張都不錯的照片" in body
    assert "相對鑑別分＝原始分 35%＋照片庫百分位 65%" in body
    assert "排序原始分 80.0 → 相對鑑別 80.0" in body
    assert "選片分 —（尚未正式分析）" in body
    assert "E6 顯示適合度 91.9（暫估，未納入正式選片分）" in body


def test_photo_detail_shows_only_two_latest_analyses_and_compact_type_picker(client, app):
    create_admin(app)
    login(client)
    photo_id = add_photos(app, 1)[0]
    for index in range(3):
        result = valid_result(caption=f"第 {index + 1} 次分析")
        app.extensions["inktime_photo_repository"].save_analysis(
            photo_id,
            None,
            "local",
            "local",
            "local",
            result,
            "{}",
            ranking_score=50 + index,
        )

    body = client.get(f"/photos/{photo_id}").get_data(as_text=True)

    assert body.count('class="analysis-card"') == 2
    assert "只顯示最新 2 / 3 筆" in body
    assert "單純掃描不一定新增" in body
    assert 'class="photo-orientation-actions"' in body
    assert 'class="secondary orientation-set orientation-clear"' in body
    assert 'class="photo-type-picker"' in body
    assert "第 3 次分析" in body
    assert "第 2 次分析" in body
    assert "第 1 次分析" not in body


def test_ai_trace_page_explains_that_only_real_provider_calls_are_listed(client, app):
    create_admin(app)
    login(client)

    body = client.get("/ai/traces").get_data(as_text=True)

    assert "這裡不是照片庫" in body
    assert "哪些照片會出現在這裡" in body
    assert "真的開始呼叫外部或測試 Provider" in body
    assert "在呼叫 Provider 之前就被預算或設定阻擋" in body
    assert "不同照片" in body


def test_photo_cards_never_present_excluded_screenshot_or_severe_blur_as_high_score(client, app):
    create_admin(app)
    login(client)
    screenshot_id, blurry_id = add_photos(app, 2)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            UPDATE photos SET relative_path='截圖 2026-06-28.png',width=1034,height=802,format='PNG',
                camera_make=NULL,camera_model=NULL,screenshot_likelihood=1.0,blur_score=1638.33,
                contrast=27.51,e6_score=98.6,local_features_status='complete',
                local_candidate_score=100,eligible=1,exclusion_status='eligible'
            WHERE id=?
            """,
            (screenshot_id,),
        )
        connection.execute(
            """
            UPDATE photos SET relative_path='_DSC0007.jpg',width=6000,height=4000,format='JPEG',
                camera_make='SONY',screenshot_likelihood=0,blur_score=44.54,contrast=11.80,
                e6_score=100,local_features_status='complete',local_candidate_score=42.8,
                eligible=1,exclusion_status='eligible'
            WHERE id=?
            """,
            (blurry_id,),
        )

    listing = client.get("/photos").get_data(as_text=True)
    assert "選片分 0.0（已排除：截圖）" in listing
    assert "選片分 0.0（已排除：嚴重模糊／失焦）" in listing
    assert "本機品質" in listing
    assert "選片分只在正式排序分析完成後產生" in listing
    assert "E6 不會將它救回" in listing

    detail = client.get(f"/photos/{blurry_id}").get_data(as_text=True)
    assert "本機品質分" in detail
    assert "模糊 44.54／對比 11.80" in detail
    assert "模糊 &lt; 60 且對比 &lt; 15" in detail
    assert "明確截圖或嚴重單項缺陷會直接排除" in detail


def test_photo_cards_force_ineligible_selection_score_to_zero_but_keep_diagnostics(client, app):
    create_admin(app)
    login(client)
    photo_id = add_photos(app, 1)[0]
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE photos SET eligible=0,e6_score=99,exclusion_status='eligible' WHERE id=?",
            (photo_id,),
        )
    app.extensions["inktime_photo_repository"].save_analysis(
        photo_id,
        None,
        "stage_one",
        "測試 Provider",
        "vision-model",
        valid_result(),
        "{}",
        ranking_score=98,
    )

    body = client.get("/photos").get_data(as_text=True)

    assert "選片分 0.0（已排除：" in body
    assert "模型排序 98.0" in body
    assert "E6 顯示適合度 99.0" in body


def test_photo_library_loads_200_per_page_and_keeps_filters(client, app):
    create_admin(app)
    login(client)
    photo_ids = add_photos(app, 201)
    with app.extensions["inktime_database"].session() as connection:
        connection.executemany(
            "UPDATE photos SET status='analyzed' WHERE id=?",
            [(photo_id,) for photo_id in photo_ids],
        )

    first = client.get("/photos?status=analyzed")
    first_body = first.get_data(as_text=True)
    assert first.status_code == 200
    assert first_body.count('class="photo-card"') == 200
    assert "目前顯示第 1–200 張" in first_body
    assert "第 1 / 2 頁" in first_body
    assert "status=analyzed&amp;page=2" in first_body
    assert '<option value="analyzed" selected>' in first_body

    second = client.get("/photos?status=analyzed&page=2")
    second_body = second.get_data(as_text=True)
    assert second.status_code == 200
    assert second_body.count('class="photo-card"') == 1
    assert "目前顯示第 201–201 張" in second_body
    assert "第 2 / 2 頁" in second_body

    clamped = client.get("/photos?status=analyzed&page=999")
    clamped_body = clamped.get_data(as_text=True)
    assert clamped.status_code == 200
    assert clamped_body.count('class="photo-card"') == 1
    assert "第 2 / 2 頁" in clamped_body


def test_review_thumbnail_accepts_string_root_and_rejects_invalid_sources(
    client, app, tmp_path, monkeypatch
):
    create_admin(app)
    login(client)
    photo_id = add_photos(app, 1)[0]
    root = tmp_path / "review-source"
    root.mkdir()
    Image.new("RGB", (80, 60), "#527f99").save(root / "photo.jpg")
    source_sha256 = sha256((root / "photo.jpg").read_bytes()).hexdigest()
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE libraries SET root_path=? WHERE id=(SELECT library_id FROM photos WHERE id=?)",
            (str(root), photo_id),
        )
        connection.execute(
            "UPDATE photos SET relative_path='photo.jpg',sha256=? WHERE id=?",
            (source_sha256, photo_id),
        )

    valid = client.get(f"/api/v1/review/photos/{photo_id}/thumbnail")
    assert valid.status_code == 200
    assert valid.mimetype == "image/jpeg"

    (root / "photo.jpg").unlink()
    assert client.get(f"/api/v1/review/photos/{photo_id}/thumbnail").status_code == 404

    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET relative_path='../outside.jpg' WHERE id=?", (photo_id,))
    assert client.get(f"/api/v1/review/photos/{photo_id}/thumbnail").status_code == 400

    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET relative_path='photo.jpg' WHERE id=?", (photo_id,))
    Image.new("RGB", (80, 60), "#527f99").save(root / "photo.jpg")
    monkeypatch.setattr(
        app.extensions["inktime_thumbnail_cache"],
        "get_or_create",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("thumbnail failed")),
    )
    assert client.get(f"/api/v1/review/photos/{photo_id}/thumbnail").status_code == 422


def test_rendering_console_exposes_layout_e6_and_manual_crop_controls(client, app, tmp_path):
    app.extensions["inktime_settings_repository"].update(
        "analysis.execution_mode", "automatic_ai", changed_by="test", source_ip="127.0.0.1"
    )
    create_admin(app)
    login(client)
    photo_id = add_photos(app, 1)[0]
    photo_root = tmp_path / "rendering-preview"
    photo_root.mkdir()
    Image.new("RGB", (900, 600), "#527f99").save(photo_root / "0.jpg")
    result = valid_result()
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE libraries SET root_path=? WHERE id=(SELECT library_id FROM photos WHERE id=?)",
            (str(photo_root), photo_id),
        )
        connection.execute(
            """
            UPDATE photos SET status='analyzed',captured_at='2020-07-20T10:00:00',
                e6_score=82,e6_contrast_score=84,e6_subject_score=80,e6_skin_score=78,
                e6_text_score=86,crop_focus_x=.72,crop_focus_y=.38,crop_method='saliency'
            WHERE id=?
            """,
            (photo_id,),
        )
    app.extensions["inktime_photo_repository"].save_analysis(
        photo_id,
        None,
        "stage_one",
        "測試 Provider",
        "vision-model",
        result,
        "{}",
        ranking_score=88,
    )

    page = client.get("/rendering")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "智慧裁切與版型預覽" in body
    assert "相框方向與空間利用" in body
    assert "完整顯示" in body
    assert "完整顯示（建議）" not in body
    assert "背景工作未提供 Preview 結果" in body
    assert "URL.revokeObjectURL" in body
    assert "new AbortController" in body
    assert "compositionPreviewController?.abort()" in body
    assert "effectiveFit=adaptive?'contain':frameFitMode.value" in body
    assert "fit_mode:adaptive?'contain':frameFitMode.value" in body
    assert "雙照片拼版" in body
    assert "月曆相框" in body
    assert "天氣＋室內溫溼度" in body
    assert "E6 總分" in body
    assert "歷年今日優先" in body

    landscape = client.get(
        f"/api/v1/rendering/preview/{photo_id}?layout=photo_info&orientation=landscape&fit_mode=contain"
    )
    assert landscape.status_code == 202
    created = landscape.get_json()
    WorkerRunner(app).run_once()
    status = client.get(created["status_url"])
    assert status.status_code == 200
    completed = client.get(status.get_json()["result"]["preview_url"])
    assert completed.status_code == 200
    assert Image.open(BytesIO(completed.data)).size == (800, 480)

    invalid_orientation = client.get(f"/api/v1/rendering/preview/{photo_id}?orientation=diagonal")
    assert invalid_orientation.status_code == 400

    response = client.patch(
        f"/api/v1/photos/{photo_id}/crop",
        json={"mode": "manual", "x": 0.2, "y": 0.8},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 200
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT crop_manual_x,crop_manual_y FROM photos WHERE id=?", (photo_id,)
        ).fetchone()
    assert tuple(row) == (0.2, 0.8)


def test_photo_detail_backfills_local_e6_and_crop_without_model(client, app, tmp_path):
    create_admin(app)
    login(client)
    root = tmp_path / "legacy-photo"
    root.mkdir()
    Image.new("RGB", (900, 600), "#587d98").save(root / "memory.jpg")
    repository = app.extensions["inktime_photo_repository"]
    library_id = repository.ensure_library("舊照片", Path(root))
    now = "2026-07-20T00:00:00+00:00"
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            INSERT INTO photos(id,library_id,relative_path,status,created_at,updated_at)
            VALUES ('legacy-photo',?,?,'analyzed',?,?)
            """,
            (library_id, "memory.jpg", now, now),
        )
    repository.save_analysis("legacy-photo", None, "stage_one", "test", "vision", valid_result(), "{}")

    page = client.get("/photos/legacy-photo")

    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "E6 適合度" in body
    assert "原始照片目前無法讀取" not in body
    with app.extensions["inktime_database"].session() as connection:
        photo = connection.execute(
            "SELECT e6_score,crop_focus_x,crop_method FROM photos WHERE id='legacy-photo'"
        ).fetchone()
        usage_count = connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0]
    assert photo["e6_score"] is not None
    assert photo["crop_focus_x"] is not None
    assert photo["crop_method"] in {"faces", "saliency"}
    assert usage_count == 0
