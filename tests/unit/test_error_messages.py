from __future__ import annotations

import ast
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from werkzeug.exceptions import TooManyRequests

from inktime.app.api.jobs import _job_item_view
from inktime.app.core.errors import ApplicationError
from inktime.app.web.error_messages import CATALOG, DEFAULT, error_text, explain_error, plain_message
from tests.conftest import create_admin, login


ROOT = Path(__file__).resolve().parents[2]
NUMBERED_CODE = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{3}\b")
ERROR_PREFIXES = (
    "AI-", "ANALYSIS-", "BATCH-", "DEVICE-", "DISPLAY-", "JOB-", "SCAN-", "NOTIFY-",
    "QUEUE-", "VLM-", "SCHEDULE-", "RENDER-", "PAIR-", "DISK-", "HTTP-", "DB-",
    "BUDGET-", "PREFLIGHT-", "RESTORE-", "RETENTION-", "IMG-",
)


class PrimaryText(HTMLParser):
    """Ignore collapsed diagnostics and script/style text, as a user initially sees it."""

    def __init__(self):
        super().__init__()
        self.hidden = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "details"}:
            self.hidden.append(tag)

    def handle_endtag(self, tag):
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def primary_text(html):
    parser = PrimaryText()
    parser.feed(html)
    return " ".join(parser.parts)


def test_every_source_numbered_error_and_named_error_family_has_an_explanation():
    found = set()
    for path in (ROOT / "inktime").rglob("*.py"):
        if path.name == "error_messages.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.update(NUMBERED_CODE.findall(node.value))
                if (node.value.startswith(ERROR_PREFIXES)
                        and re.fullmatch(r"[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+", node.value)):
                    found.add(node.value)
    missing = sorted(found - CATALOG.keys() - {"SHA-256"})
    assert not missing, f"Missing error explanations: {missing}"


@pytest.mark.parametrize("code", sorted(CATALOG))
def test_catalog_has_complete_chinese_explanations_and_hides_identifiers(code):
    explanation = explain_error(code)
    assert explanation["known"]
    for field in ("title", "detail", "action"):
        assert re.search(r"[\u3400-\u9fff]", explanation[field])
    assert code not in explanation["message"]
    assert explanation["code"] == code
    assert error_text(code) == explanation["message"]


def test_unknown_error_is_honest_and_never_suggests_blind_retries():
    error = explain_error("FUTURE-999", "UnknownProviderFailure")
    assert not error["known"]
    assert "尚未分類" in error["title"]
    assert "不能只由代碼判斷原因" in error["detail"]
    assert "避免重複提交" in error["action"]
    assert "FUTURE-999" not in error["message"]
    assert error["technical_message"] == "UnknownProviderFailure"


def test_schema_error_and_specific_validation_are_explained_without_losing_evidence():
    schema_error = explain_error("VLM-004", "visual_orientation 格式不合法")
    assert schema_error["title"] == "模型回覆的欄位格式不符合要求"
    assert "visual_orientation 格式不合法" in schema_error["detail"]
    assert "至少需要 12 個字元" in error_text("password_too_short", "密碼至少需要 12 個字元。")
    assert "SHOULD-NOT-LEAK" not in error_text("IMG-004", "SHOULD-NOT-LEAK 欄位不合法")


@pytest.mark.parametrize("status,title", [
    (401, "模型服務拒絕驗證"), (403, "模型服務拒絕驗證"),
    (402, "模型服務的帳號額度不足"), (429, "模型服務的使用頻率已達上限"),
    (503, "模型服務端暫時發生故障"),
])
def test_provider_http_context_distinguishes_auth_quota_rate_limit_and_outage(status, title):
    assert explain_error("VLM-007", "ProviderHTTPError", status)["title"] == title


def test_reused_error_code_does_not_misdiagnose_privacy_rejection_as_provider_failure():
    error = explain_error("VLM-008", "已確認為截圖；為保護隱私與額度，禁止送入 AI 模型")
    assert error["title"] == "這張照片不允許傳送給模型"
    assert "不是模型故障" in error["detail"]
    disabled = explain_error("VLM-008", "AI 模式目前為關閉；不會建立模型工作")
    assert disabled["title"] == CATALOG["ANALYSIS-DISABLED"]["title"]
    endpoint = explain_error("VLM-005", "No endpoints found that can handle the requested parameters", 404)
    assert "沒有相容端點" in endpoint["title"]
    assert "增加 Worker 不會解除" in endpoint["action"]


def test_diagnostics_redact_secrets_paths_images_and_do_not_surface_html_or_json():
    raw = '回覆 <script>alert(1)</script> {"password":"never-show-password"} Bearer never-show-bearer /Users/person/private.jpg'
    error = explain_error("VLM-007", raw)
    for secret in ("never-show-password", "never-show-bearer", "/Users/person/private.jpg"):
        assert secret not in json.dumps(error, ensure_ascii=False)
    assert "<script>" not in error["message"]
    assert "alert(1)" not in error["message"]
    assert len(explain_error("VLM-007", "文字" * 4000)["technical_message"]) <= 1500
    image = explain_error("VLM-007", "data:image/jpeg;base64," + "A" * 1000)
    assert "A" * 256 not in image["technical_message"]


def test_information_events_remain_information_and_catalog_is_not_mutated():
    before = json.dumps(CATALOG, sort_keys=True)
    assert plain_message("DISK-CRITICAL 已恢復") == "資料磁碟的使用比例過高 已恢復"
    assert plain_message("工作已完成") == "工作已完成"
    explain_error("VLM-004", "回傳欄位不合法")
    assert json.dumps(CATALOG, sort_keys=True) == before


def test_job_final_success_never_presents_old_failure_as_current():
    item = {"status": "completed", "stage": "single", "attempts": 3, "error_code": "VLM-005"}
    explanation = _job_item_view(item)
    assert explanation["explanation_title"] == "已成功"
    assert "user_error" not in explanation
    item.update(status="failed", error_message="ProviderHTTPError", latest_http_status=429,
                latest_provider_response='{"error":{"message":"rate limited"}}')
    failed = _job_item_view(item)
    assert failed["explanation_title"] == "模型服務的使用頻率已達上限"
    assert failed["explanation_action"]


def test_error_json_adds_presentation_but_preserves_machine_contract_and_retry_after(client, app):
    @app.get("/api/v1/test-human-error")
    def failed_request():
        raise TooManyRequests(description="VLM-002 模型服務暫時限制請求", retry_after=17)

    @app.get("/api/v1/test-nested-error")
    def nested_failure():
        raise ApplicationError("密碼至少需要 12 個字元。", code="password_too_short")

    @app.get("/api/v1/test-success-payload")
    def successful_payload():
        return {"error_code": "VLM-004", "raw": {"content": "VLM-005"}}

    create_admin(app)
    login(client)
    response = client.get("/api/v1/test-human-error")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "17"
    assert response.json["error_code"] == "VLM-002"
    assert response.json["message"] == "模型服務暫時限制請求"
    assert response.json["user_error"]["title"] == CATALOG["VLM-002"]["title"]
    nested = client.get("/api/v1/test-nested-error").json
    assert nested["error"] == {"code": "password_too_short", "message": "密碼至少需要 12 個字元。"}
    assert "至少需要 12 個字元" in nested["user_error"]["message"]
    assert client.get("/api/v1/test-success-payload").json == {
        "error_code": "VLM-004", "raw": {"content": "VLM-005"},
    }
    # Firmware protocol errors remain byte-structure compatible, without UI metadata.
    device = client.get("/api/device/v1/releases/latest")
    assert device.status_code == 401
    assert device.json == {"error_code": "DEVICE-001", "message": "裝置驗證失敗"}


def test_error_pages_flash_and_error_center_show_words_with_codes_only_collapsed(client, app):
    @app.get("/test-flash-human-error")
    def flash_failure():
        raise ApplicationError("密碼至少需要 12 個字元。", code="password_too_short")

    create_admin(app)
    login(client)
    missing = client.get("/nonexistent-human-error-page")
    assert missing.status_code == 404
    assert "找不到這項內容" in primary_text(missing.text)
    assert "HTTP-404" not in primary_text(missing.text)
    assert "HTTP-404" in missing.text
    redirect = client.get("/test-flash-human-error")
    assert redirect.status_code == 303
    with client.session_transaction() as session:
        assert session["_flashes"] == [
            ("error:password_too_short", "密碼至少需要 12 個字元。")
        ]
    flashed = client.get(redirect.headers["Location"])
    assert "密碼至少需要 12 個字元" in primary_text(flashed.text)
    assert "password_too_short" not in primary_text(flashed.text)
    assert 'class="notice error"' in flashed.text
    app.extensions["inktime_observability_service"].alert(
        "provider", "VLM-004", "visual_orientation 格式不合法", severity="ERROR",
    )
    page = client.get("/errors")
    assert page.status_code == 200
    assert "模型回覆的欄位格式不符合要求" in primary_text(page.text)
    assert "可以怎麼處理" in primary_text(page.text)
    assert "VLM-004" not in primary_text(page.text)
    assert '<details class="error-technical">' in page.text


def test_catalog_is_public_cacheable_and_not_duplicated_in_every_page(client, app):
    response = client.get("/ui/error-catalog.js")
    assert response.status_code == 200
    assert response.mimetype == "application/javascript"
    assert "must-revalidate" in response.headers["Cache-Control"]
    assert client.get("/ui/error-catalog.js", headers={"If-None-Match": response.headers["ETag"]}).status_code == 304
    config = json.loads(response.text.removeprefix("window.inktimeErrorCatalog=").removesuffix(";"))
    assert config == {"entries": CATALOG, "fallback": DEFAULT}
    create_admin(app)
    login(client)
    page = client.get("/dashboard").text
    assert '/ui/error-catalog.js' in page
    assert 'id="inktime-error-catalog"' not in page
    assert page.index('/ui/error-catalog.js') < page.index('/static/error-messages.js')
    receiver = (ROOT / "inktime/app/web/templates/virtual_display.html").read_text()
    assert receiver.index("error_catalog_script") < receiver.index("error-messages.js") < receiver.index("virtual-display.js")
    assert receiver.index("virtual-display.js") < receiver.index("</body>")


def test_browser_error_helpers_match_server_and_preserve_success_payloads():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the hosted JavaScript contract check")
    source = (ROOT / "inktime/app/web/static/error-messages.js").read_text()
    cases = [[code, "", None] for code in CATALOG] + [
        ["VLM-004", "visual_orientation 格式不合法", 200],
        ["VLM-008", "已確認為截圖；禁止送入 AI 模型", 409],
        ["VLM-008", "AI 模式目前為關閉", 409],
        ["VLM-005", "No endpoints found that can handle the requested parameters", 404],
        ["VLM-007", "ProviderHTTPError", 401],
        ["VLM-007", "ProviderHTTPError", 402],
        ["VLM-007", "ProviderHTTPError", 429],
        ["VLM-007", "ProviderHTTPError", 503],
        ["", "VLM-004 欄位不合法", None], ["", "Failed to fetch", None],
        ["", "必填欄位未填寫", None], ["FUTURE-999", "unknown", None],
        ["constructor", "unknown", None], ["__proto__", "unknown", None],
    ]
    script = """
const vm = require('node:vm');
const sandbox = {window:{inktimeErrorCatalog:config}, URL};
vm.runInNewContext(source, sandbox);
const w = sandbox.window;
const response = (ok, text, status=ok?200:400)=>({ok,status,text:async()=>text});
(async()=>{
  const explanations = cases.map(args=>w.inktimeExplainError(...args));
  const html = w.inktimeErrorHtml('VLM-004','<img src=x onerror=alert(1)>');
  const success = await w.inktimeDecodeJson(response(true,'{"error_code":"VLM-004","raw":{"text":"untouched"}}'));
  const nested = await w.inktimeDecodeJson(response(false,'{"error":{"code":"password_too_short","message":"密碼至少需要 12 個字元。"}}'));
  const invalid = await w.inktimeDecodeJson(response(false,'<html>proxy secret</html>'));
  let malformedSuccessRejected=false;
  try { await w.inktimeDecodeJson(response(true,'<html>login</html>')); } catch (_) { malformedSuccessRejected=true; }
  const safe = w.inktimeExplainError('VLM-007','回覆 {"password":"never-show"} Bearer private-bearer /Users/person/private.jpg');
  const offlineSandbox = {window:{}, URL};
  vm.runInNewContext(source, offlineSandbox);
  const missingCatalog = offlineSandbox.window.inktimeExplainError('VLM-007','ProviderHTTPError',429);
  process.stdout.write(JSON.stringify({explanations,html,success,nested,invalid,malformedSuccessRejected,safe,missingCatalog}));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    # Fixed source-owned program only; no provider, network or browser process.
    script = (
        "const config=" + json.dumps({"entries": CATALOG, "fallback": DEFAULT}, ensure_ascii=False) + ";\n"
        + "const source=" + json.dumps(source) + ";\n"
        + "const cases=" + json.dumps(cases, ensure_ascii=False) + ";\n" + script
    )
    completed = subprocess.run([node, "-"], input=script, text=True, capture_output=True, check=True, timeout=30)  # noqa: S603
    result = json.loads(completed.stdout)
    for args, actual in zip(cases, result["explanations"], strict=True):
        expected = explain_error(*args)
        for field in ("title", "detail", "action", "message", "code", "known"):
            assert actual[field] == expected[field], (args, field)
    assert "<img" not in result["html"]
    assert "&lt;img" in result["html"]
    assert "VLM-004" not in primary_text(result["html"])
    assert result["success"] == {"error_code": "VLM-004", "raw": {"text": "untouched"}}
    assert result["nested"]["error"]["code"] == "password_too_short"
    assert "密碼至少需要 12 個字元" in result["nested"]["message"]
    assert "proxy secret" not in json.dumps(result["invalid"])
    assert result["malformedSuccessRejected"]
    assert "never-show" not in json.dumps(result["safe"])
    assert "private-bearer" not in json.dumps(result["safe"])
    assert "/Users/person" not in json.dumps(result["safe"])
    assert "無法載入完整錯誤說明" in result["missingCatalog"]["title"]
