from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from inktime.app.core.ai_trace_payloads import bounded_json_text, bounded_text, sanitize_trace_value
from inktime.app.providers.base import ProviderCallTrace
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider
from tests.conftest import create_admin, login


def _seed_trace(app, *, status: str = "RUNNING") -> str:
    now = datetime.now(timezone.utc).isoformat()
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "INSERT INTO ai_trace_runs(trace_id,stage,status,started_at,created_at) VALUES ('trace-test','single',?,?,?)",
            (status, now, now),
        )
        connection.execute(
            """
            INSERT INTO ai_trace_attempts(
                trace_id,attempt_number,attempt_kind,provider,requested_model,status,
                request_json_sanitized,response_raw_sanitized,response_parsed_json,created_at
            ) VALUES ('trace-test',1,'vision','provider','requested-model','RUNNING',?,?,?,?)
            """,
            (
                json.dumps({"model": "requested-model", "authorization": "never-store"}),
                "raw-output",
                json.dumps({"caption": "parsed"}),
                now,
            ),
        )
    return "trace-test"


def test_trace_sanitizer_redacts_secrets_images_depth_and_size():
    secret = "sk-super-secret-provider-key"
    value = {
        "Authorization": f"Bearer {secret}",
        "x-api-key": secret,
        "password": "password-value",
        "cookie": "session-cookie-value",
        "client_secret": "client-secret-value",
        "image": "data:image/jpeg;base64," + "A" * 2000,
        "nested": {"a": {"b": {"c": {"d": {"e": {"f": "too-deep"}}}}}},
    }
    clean = sanitize_trace_value(value)
    persisted = bounded_json_text(value, maximum_bytes=1024)
    combined = json.dumps(clean, ensure_ascii=False) + persisted
    assert secret not in combined
    assert "password-value" not in combined
    assert "session-cookie-value" not in combined
    assert "client-secret-value" not in combined
    assert "A" * 256 not in combined
    assert "已遮蔽" in combined
    assert len(persisted.encode("utf-8")) <= 1024
    raw = bounded_text(json.dumps({"password": "raw-password", "content": value["image"]}))
    assert "raw-password" not in raw and "A" * 256 not in raw


def test_trace_repository_keeps_list_light_and_detail_role_safe(app):
    trace_id = _seed_trace(app)
    repository = app.extensions["inktime_ai_trace_repository"]
    page = repository.list_runs(filters={}, limit=500)
    assert len(page["traces"]) == 1
    assert "request_json_sanitized" not in page["traces"][0]
    assert "response_raw_sanitized" not in page["traces"][0]
    assert "final_result_json" not in page["traces"][0]
    admin = repository.detail(trace_id, include_sensitive=True)
    viewer = repository.detail(trace_id, include_sensitive=False)
    assert admin["attempts"][0]["request_json_sanitized"]
    assert admin["attempts"][0]["response_raw_sanitized"] == "raw-output"
    assert "request_json_sanitized" not in viewer["attempts"][0]
    assert "response_raw_sanitized" not in viewer["attempts"][0]
    assert viewer["attempts"][0]["response_parsed_json"] == {"caption": "parsed"}


def test_trace_foreign_keys_cascade_attempts_and_set_usage_null(app):
    trace_id = _seed_trace(app)
    now = datetime.now(timezone.utc).isoformat()
    with app.extensions["inktime_database"].session() as connection:
        usage_id = connection.execute(
            """
            INSERT INTO api_usage(
                provider,model,request_type,input_tokens,output_tokens,cached_tokens,
                estimated_cost,started_at,latency_ms,status
            ) VALUES ('provider','served-model','single',1,2,0,0,?,1,'completed')
            """,
            (now,),
        ).lastrowid
        connection.execute(
            "UPDATE ai_trace_attempts SET api_usage_id=? WHERE trace_id=?", (usage_id, trace_id)
        )
        connection.execute("DELETE FROM api_usage WHERE id=?", (usage_id,))
        assert (
            connection.execute(
                "SELECT api_usage_id FROM ai_trace_attempts WHERE trace_id=?", (trace_id,)
            ).fetchone()[0]
            is None
        )
        connection.execute("DELETE FROM ai_trace_runs WHERE trace_id=?", (trace_id,))
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM ai_trace_attempts WHERE trace_id=?", (trace_id,)
            ).fetchone()[0]
            == 0
        )


def test_trace_retention_uses_existing_bounded_dry_run_framework(app):
    _seed_trace(app)
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    with app.extensions["inktime_database"].session() as connection:
        usage_id = connection.execute(
            """
            INSERT INTO api_usage(
                provider,model,request_type,input_tokens,output_tokens,cached_tokens,
                estimated_cost,started_at,latency_ms,status
            ) VALUES ('provider','model','single',1,1,0,0,?,1,'completed')
            """,
            (old,),
        ).lastrowid
        connection.execute(
            "UPDATE ai_trace_attempts SET api_usage_id=? WHERE trace_id='trace-test'", (usage_id,)
        )
        connection.execute(
            "UPDATE ai_trace_runs SET created_at=?,started_at=? WHERE trace_id='trace-test'", (old, old)
        )
    repository = app.extensions["inktime_resilience_repository"]
    preview = repository.cleanup(dry_run=True)
    assert preview["summary"]["ai_trace"] == 1
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_trace_runs").fetchone()[0] == 1
    deleted = repository.cleanup(dry_run=False)
    assert deleted["summary"]["ai_trace"] == 1
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_trace_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM api_usage WHERE id=?", (usage_id,)).fetchone()[0] == 1


def test_trace_pages_and_json_require_auth_and_viewer_cannot_read_raw(client, app):
    create_admin(app)
    app.extensions["inktime_auth_repository"].create_user("viewer", "viewer-passphrase-long", role="viewer")
    _seed_trace(app)
    raw_output = '{"caption":"他们在复古小镇看着风景"}'
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE ai_trace_attempts SET response_raw_sanitized=? WHERE trace_id='trace-test'",
            (raw_output,),
        )
    assert client.get("/api/v1/ai/traces").status_code == 401
    assert client.get("/ai/traces").status_code == 302
    login(client, "viewer", "viewer-passphrase-long")
    page = client.get("/ai/traces")
    assert page.status_code == 200
    assert "replaceChildren" in page.get_data(as_text=True)
    assert "document.hidden" in page.get_data(as_text=True)
    detail = client.get("/api/v1/ai/traces/trace-test")
    assert detail.status_code == 200
    assert "request_json_sanitized" not in detail.json["attempts"][0]
    assert "response_raw_sanitized" not in detail.json["attempts"][0]
    admin = app.test_client()
    login(admin)
    admin_detail = admin.get("/api/v1/ai/traces/trace-test")
    assert admin_detail.status_code == 200
    assert admin_detail.json["attempts"][0]["request_json_sanitized"]
    assert admin_detail.json["attempts"][0]["response_raw_sanitized"] == raw_output
    admin_page = admin.get("/ai/traces/trace-test").get_data(as_text=True)
    assert "他們在復古小鎮看著風景" in admin_page
    assert "他们在复古小镇看着风景" not in admin_page
    assert "資料庫仍保留 Provider 原始稽核內容" in admin_page


def test_trace_attempt_preserves_requested_and_served_models(app):
    repository = app.extensions["inktime_ai_trace_repository"]
    now = datetime.now(timezone.utc).isoformat()
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "INSERT INTO ai_trace_runs(trace_id,stage,status,started_at,created_at) VALUES ('model-trace','single','RUNNING',?,?)",
            (now, now),
        )
    attempt_id = repository.start_attempt(
        "model-trace",
        attempt_kind="vision",
        provider="provider",
        provider_id="provider-id",
        requested_model="alias/model-a",
    )
    repository.update_attempt_from_call(
        attempt_id,
        ProviderCallTrace(served_model="model-b"),
        status="SUCCESS",
        served_model="model-b",
    )
    detail = repository.detail("model-trace", include_sensitive=True)
    assert detail["attempts"][0]["requested_model"] == "alias/model-a"
    assert detail["attempts"][0]["served_model"] == "model-b"


def test_trace_final_result_is_an_immutable_historical_snapshot(app):
    _seed_trace(app)
    repository = app.extensions["inktime_ai_trace_repository"]
    original = {"caption": "Result A", "ranking_score": 88}
    repository.complete_run("trace-test", status="SUCCESS", final_result=original)
    original["caption"] = "Result B"
    assert repository.detail("trace-test", include_sensitive=False)["final_result_json"] == {
        "caption": "Result A",
        "ranking_score": 88,
    }


def test_provider_captures_actual_sanitized_transport_request(tmp_path):
    class Response:
        status_code = 200
        headers = {"x-request-id": "request-123"}
        text = json.dumps(
            {
                "model": "served-model-b",
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            }
        )

        def json(self):
            return json.loads(self.text)

    class Session:
        def __init__(self):
            self.posts = []

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return Response()

        def close(self):
            pass

    image = tmp_path / "prepared.jpg"
    image.write_bytes(b"prepared-image-payload")
    session = Session()
    provider = OpenAICompatibleProvider(
        name="provider",
        provider_id="provider-id",
        base_url="https://provider.invalid/v1",
        api_key="sk-never-persist-this-key",
        session=session,
    )
    response = provider.analyze(
        image_path=image,
        model="requested-model-a",
        detail="high",
        stage="single",
        max_tokens=512,
    )
    assert len(session.posts) == 1
    assert response.served_model == "served-model-b"
    assert response.call_trace is not None
    assert response.call_trace.request_started_at is not None
    assert response.call_trace.response_received_at is not None
    assert response.call_trace.http_status == 200
    persisted = json.dumps(response.call_trace.request_json_sanitized, ensure_ascii=False)
    assert "requested-model-a" in persisted
    assert "sk-never-persist-this-key" not in persisted
    assert "prepared-image-payload" not in persisted
    assert "已遮蔽圖片資料" in persisted
