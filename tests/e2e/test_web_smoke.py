from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright
expect = playwright_api.expect


def _review_draft_and_failure_regression(page, base):
    photos = [{"id": f"review-fixture-{index}", "analysis_id": index, "review_version": 0,
               "thumbnail_url": "/fixture-review.svg", "review_state": "unreviewed", "caption_override": "",
               "score_kind": "semantic", "analysis_provider": "fixture", "analysis_model": "fixture"}
              for index in (1, 2, 3)]
    state = {"fail_patch": False, "bad_json": False, "patches": []}

    def review_route(route):
        request = route.request
        if request.method == "PATCH":
            if state["fail_patch"]:
                route.abort()
                return
            payload = request.post_data_json
            state["patches"].append(payload)
            photo = next(value for value in photos if request.url.endswith('/' + value["id"]))
            assert payload["analysis_id"] == photo["analysis_id"]
            assert payload["expected_version"] == photo["review_version"]
            photo.update({key: value for key, value in payload.items() if key not in {"expected_version", "analysis_id"}})
            photo["review_version"] += 1
            route.fulfill(json={"photo": photo})
            return
        if state["bad_json"]:
            route.fulfill(status=200, content_type="application/json", body="invalid-json")
            return
        append = bool(parse_qs(urlparse(request.url).query).get("cursor"))
        route.fulfill(json={"items": photos[2:] if append else photos[:2], "next_cursor": None if append else "next", "has_more": not append})

    page.route("**/api/v1/review/photos**", review_route)
    page.route("**/api/v1/review/summary?**", lambda route: route.fulfill(json={"total": 3, "states": {"unreviewed": 3}}))
    page.route("**/fixture-review.svg", lambda route: route.fulfill(content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"/>'))
    page.goto(base + "/review/photos")
    expect(page.locator('.review-card')).to_have_count(2)
    first = page.locator('.review-card[data-id="review-fixture-1"]')
    draft = first.locator('[data-caption]')
    draft.click()
    draft.press_sequentially("UNSAVED-DRAFT")
    expect(draft).to_be_focused()
    page.locator('.review-card[data-id="review-fixture-2"] img').click()
    page.keyboard.press("ArrowLeft")
    expect(draft).to_have_value("UNSAVED-DRAFT")
    draft.focus()
    page.evaluate("document.getElementById('review-more').click()")
    expect(page.locator('.review-card')).to_have_count(3)
    expect(draft).to_be_focused()
    expect(draft).to_have_value("UNSAVED-DRAFT")
    page.locator('.review-card[data-id="review-fixture-2"] [data-action="keep"]').click()
    expect(page.locator('#review-status')).to_contain_text('已儲存 review-fixture-2')
    expect(draft).to_have_value("UNSAVED-DRAFT")
    first.locator('[data-action="save_caption"]').click()
    expect(page.locator('#review-status')).to_contain_text('已儲存 review-fixture-1')
    assert photos[0]["caption_override"] == "UNSAVED-DRAFT"
    state["fail_patch"] = True
    draft.fill("RETAIN-ON-FAILURE")
    first.locator('[data-action="favorite"]').click()
    expect(page.locator('#review-status')).to_contain_text('儲存結果請重新載入查核')
    expect(first.locator('[data-action="favorite"]')).to_be_enabled()
    expect(draft).to_have_value("RETAIN-ON-FAILURE")
    state["bad_json"] = True
    page.locator('#review-filter').evaluate("form => form.requestSubmit()")
    expect(page.locator('#review-status')).to_contain_text('草稿保留')
    expect(page.locator('#review-more')).to_be_enabled()
    expect(draft).to_have_value("RETAIN-ON-FAILURE")
    assert len(state["patches"]) == 2
    page.unroute("**/api/v1/review/photos**", review_route)


def _crop_state_regression(page, base):
    # Execute the production controller with synthetic controls and deferred HTTP replies.
    fixture = """<select id="photo"><option value="a" data-focus-x=".25" data-focus-y=".25" data-auto-x="0" data-auto-y="1">A</option>
      <option value="b" data-focus-x=".75" data-focus-y=".75" data-auto-x=".75" data-auto-y=".75">B</option></select>
      <input id="x" type="range" min="0" max="100"><input id="y" type="range" min="0" max="100">
      <button id="save">save</button><button id="auto">auto</button><p id="status"></p>
      <script src="/static/crop-editor.js"></script><script>
      window.pendingCrop=[];window.inktimeFetch=(url,options)=>new Promise((resolve,reject)=>pendingCrop.push({url,options,resolve,reject}));
      window.inktimeDecodeJson=response=>response.json();window.inktimeSetStatus=(node,text)=>node.textContent=text;
      const photo=document.getElementById('photo'),x=document.getElementById('x'),y=document.getElementById('y');
      const refresh=reset=>{if(reset){x.value=Math.round(Number(photo.selectedOptions[0].dataset.focusX)*100);y.value=Math.round(Number(photo.selectedOptions[0].dataset.focusY)*100);}};
      photo.onchange=()=>refresh(true);refresh(true);
      inktimeBindCropEditor({photoSelect:photo,x,y,saveButton:document.getElementById('save'),autoButton:document.getElementById('auto'),status:document.getElementById('status'),refresh});
      </script>"""
    page.route("**/__crop_fixture", lambda route: route.fulfill(content_type="text/html", body=fixture))
    page.goto(base + "/__crop_fixture")
    page.locator('#x').press('End')
    page.locator('#y').press('End')
    page.locator('#save').click()
    page.locator('#photo').select_option('b')
    page.evaluate("pendingCrop.shift().resolve(new Response(JSON.stringify({crop:{photo_id:'a',focus_x:1,focus_y:1,auto_x:0,auto_y:1,manual:true}})))")
    expect(page.locator('#save')).to_be_enabled()
    expect(page.locator('#x')).to_have_value('75')
    page.locator('#photo').select_option('a')
    expect(page.locator('#x')).to_have_value('100')
    page.locator('#save').click()
    assert page.evaluate("JSON.parse(pendingCrop[0].options.body).x") == 1
    page.locator('#x').press('Home')  # A newer unsaved slider edit while the request is pending.
    page.evaluate("pendingCrop.shift().resolve(new Response(JSON.stringify({crop:{photo_id:'a',focus_x:1,focus_y:1,manual:true}})))")
    expect(page.locator('#save')).to_be_enabled()
    expect(page.locator('#x')).to_have_value('0')
    page.locator('#auto').click()
    page.locator('#photo').select_option('b')
    page.evaluate("pendingCrop.shift().resolve(new Response(JSON.stringify({crop:{photo_id:'a',focus_x:0,focus_y:1,auto_x:0,auto_y:1,manual:false}})))")
    expect(page.locator('#auto')).to_be_enabled()

    expect(page.locator('#x')).to_have_value('75')
    page.locator('#photo').select_option('a')
    expect(page.locator('#x')).to_have_value('0')
    expect(page.locator('#y')).to_have_value('100')
    page.locator('#save').click()
    page.evaluate("pendingCrop.shift().reject(new Error('synthetic network failure'))")
    expect(page.locator('#status')).to_contain_text('請查核此照片的儲存結果')
    expect(page.locator('#save')).to_be_enabled()
    expect(page.locator('#auto')).to_be_enabled()


def _action_failure_regression(page, base):
    failure = {"invalid_json": False, "calls": 0}
    def fail(route):
        failure["calls"] += 1
        if failure["invalid_json"]:
            route.fulfill(status=200, content_type="application/json", body="invalid-json")
        else:
            route.abort()
    page.route("**/api/v1/maintenance/virtual-display", fail)
    page.goto(base + "/maintenance")
    button = page.locator('#publish-virtual')
    button.click()
    expect(page.locator('#virtual-status')).to_contain_text('建立結果尚未確認')
    expect(button).to_be_enabled()
    failure["invalid_json"] = True
    button.click()
    expect(button).to_be_enabled()
    expect(page.locator('#virtual-status')).to_contain_text('避免重複建立')
    assert failure["calls"] == 2  # Only the two explicit clicks; no automatic resend.
    page.evaluate("""async () => {
      const button=document.createElement('button'),status=document.createElement('p');
      button.id='error-fixture-action';status.id='error-fixture-status';document.body.append(button,status);
      await inktimeRunAction({button,status},async()=>{throw Error('synthetic resolve failure');});
    }""")
    expect(page.locator('#error-fixture-action')).to_be_enabled()
    expect(page.locator('#error-fixture-status')).to_contain_text('操作結果尚未確認')
    page.unroute("**/api/v1/maintenance/virtual-display", fail)


def _virtual_display_failure_regression(page, base):
    payloads = {"frame-1.bin": b"\x1b", "frame-2.bin": b"\xe4"}
    state = {"fail_second": True, "files": []}
    manifest = {"release_id": "virtual-fixture", "render_profile": "safe_4c", "width": 2, "height": 2,
                "pixel_format": "2bpp", "created_at": "2026-10-13T00:00:00+00:00", "dither": "none",
                "download_base_url": "/__virtual-payload/", "palette": [
                    {"code": 0, "name": "black", "rgb": [0, 0, 0]}, {"code": 1, "name": "white", "rgb": [255, 255, 255]},
                    {"code": 2, "name": "yellow", "rgb": [255, 255, 0]}, {"code": 3, "name": "red", "rgb": [255, 0, 0]},
                ], "files": [{"name": name, "size": len(data), "sha256": sha256(data).hexdigest()} for name, data in payloads.items()]}
    page.route("**/api/v1/virtual-display/manifest?**", lambda route: route.fulfill(json=manifest))
    def payload(route):
        name = urlparse(route.request.url).path.rsplit('/', 1)[-1]
        state["files"].append(name)
        if name == "frame-2.bin" and state["fail_second"]:
            route.fulfill(status=503, content_type="application/json", json={"message": "synthetic payload failure"})
        else:
            data = payloads[name]
            route.fulfill(content_type="application/octet-stream", body=data, headers={"X-InkTime-Payload-SHA256": sha256(data).hexdigest()})
    page.route("**/__virtual-payload/**", payload)
    page.goto(base + "/virtual-display")
    expect(page.locator('#stat-frames')).to_have_text('1')
    pixels = page.locator('#display-canvas').evaluate("canvas => Array.from(canvas.getContext('2d').getImageData(0,0,2,2).data)")
    page.locator('#next-frame').click()
    expect(page.locator('#connection-state')).to_have_text('切換失敗')
    expect(page.locator('#frame-control-status')).to_have_text('第 1 / 2 張')
    expect(page.locator('#meta-file')).to_have_text('frame-1.bin')
    assert page.locator('#display-canvas').evaluate("canvas => Array.from(canvas.getContext('2d').getImageData(0,0,2,2).data)") == pixels
    polls = int(page.locator('#stat-polls').inner_text())
    page.wait_for_function("previous => Number(document.getElementById('stat-polls').textContent)>previous", arg=polls, timeout=15000)
    expect(page.locator('#connection-state')).to_have_text('切換失敗')
    state["fail_second"] = False
    page.locator('#next-frame').click()
    expect(page.locator('#stat-frames')).to_have_text('2')
    expect(page.locator('#frame-control-status')).to_have_text('第 2 / 2 張')
    expect(page.locator('#meta-file')).to_have_text('frame-2.bin')
    assert state["files"] == ['frame-1.bin', 'frame-2.bin', 'frame-2.bin']
    assert page.locator('#display-canvas').evaluate("canvas => Array.from(canvas.getContext('2d').getImageData(0,0,2,2).data)") != pixels


def _p3_filter_clear_regression(page, base):
    page.route("**/api/v1/activity?*", lambda route: route.fulfill(json={"events": [], "next_cursor": "0:0:0:0", "summary": {}}))
    page.goto(base + "/activity?job_id=p3-job&severity=ERROR&photo_id=p3-photo")
    expect(page.locator('[name="job_id"]')).to_have_value("p3-job")
    with page.expect_response(lambda response: "/api/v1/activity?" in response.url and not parse_qs(urlparse(response.url).query).get("job_id")):
        page.locator("#clear-filters").click()
    assert urlparse(page.url).query == ""
    expect(page.locator('[name="job_id"]')).to_have_value("")
    expect(page.locator('[name="photo_id"]')).to_have_value("")
    expect(page.locator('[name="severity"]')).to_have_value("")
    expect(page.locator("#auto-refresh")).to_be_checked()
    page.unroute("**/api/v1/activity?*")

    page.route("**/api/v1/ai/traces?*", lambda route: route.fulfill(json={"traces": []}))
    page.goto(base + "/ai/traces?job_id=p3-job&photo_id=p3-photo&provider=p3-provider&model=p3-model&status=SUCCESS")
    expect(page.locator('[name="job_id"]')).to_have_value("p3-job")
    with page.expect_response(lambda response: "/api/v1/ai/traces?" in response.url and not parse_qs(urlparse(response.url).query).get("job_id")):
        page.locator("#clear-traces").click()
    assert urlparse(page.url).query == ""
    for name in ("photo_id", "job_id", "provider", "model", "status"):
        expect(page.locator(f'[name="{name}"]')).to_have_value("")
    page.evaluate("""() => {
      window.p3Calls=[]; window.p3StaleRendered=false;
      const fetchOriginal=window.fetch, renderOriginal=window.renderTraceSnapshot;
      window.renderTraceSnapshot=items=>{if(items.some(item=>item.trace_id==='p3-stale'))window.p3StaleRendered=true;renderOriginal(items);};
      window.fetch=(url,...options)=>{if(String(url).startsWith('/api/v1/ai/traces?')){
        window.p3Calls.push(String(url));if(window.p3Calls.length===1)return new Promise(resolve=>window.p3Resolve=resolve);
      }return fetchOriginal(url,...options);};
      document.querySelector('[name="job_id"]').value='p3-old-request';
      document.querySelector('#trace-filters').requestSubmit();
    }""")
    page.wait_for_function("() => window.p3Calls.length === 1")
    page.locator("#clear-traces").click()
    with page.expect_response(lambda response: "/api/v1/ai/traces?" in response.url and not parse_qs(urlparse(response.url).query).get("job_id")):
        page.evaluate("window.p3Resolve(new Response(JSON.stringify({traces:[{trace_id:'p3-stale',status:'FAILED'}]}), {headers:{'Content-Type':'application/json'}}))")
        page.wait_for_function("() => window.p3Calls.length >= 2")
    assert not parse_qs(urlparse(page.evaluate("window.p3Calls[1]")).query).get("job_id")
    assert page.evaluate("window.p3StaleRendered") is False
    assert urlparse(page.url).query == ""
    page.unroute("**/api/v1/ai/traces?*")


@pytest.mark.skipif(not os.environ.get("INKTIME_E2E_URL"), reason="只在 E2E 環境執行")
def test_first_setup_login_and_primary_console_pages():
    base = os.environ["INKTIME_E2E_URL"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        csp_violations: list[str] = []
        javascript_errors: list[str] = []
        page.on("pageerror", lambda error: javascript_errors.append(str(error)))
        page.on(
            "console",
            lambda message: csp_violations.append(message.text)
            if "Content Security Policy" in message.text
            or "violates the following Content Security Policy directive" in message.text
            else None,
        )
        page.goto(base + "/setup")
        page.get_by_label("管理員帳號").fill("e2e-admin")
        page.get_by_label("密碼", exact=True).fill("e2e-password-long")
        page.get_by_label("再次輸入密碼").fill("e2e-password-long")
        page.get_by_role("button", name="建立並進入 InkTime").click()
        page.wait_for_url("**/dashboard")
        for title in ("必須完成的設定", "非必要但建議查看", "依照需求調整"):
            expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
        artifact_dir = os.environ.get("INKTIME_UI_ARTIFACT_DIR")
        if artifact_dir:
            Path(artifact_dir).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(artifact_dir) / "dashboard-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        if artifact_dir:
            page.screenshot(path=str(Path(artifact_dir) / "dashboard-mobile.png"), full_page=True)
        page.set_viewport_size({"width": 1280, "height": 900})
        operation_keys = page.evaluate("""() => {
          const descriptor = Object.getOwnPropertyDescriptor(Crypto.prototype, 'randomUUID');
          Object.defineProperty(Crypto.prototype, 'randomUUID', {value: undefined, configurable: true});
          try {
            const first = window.inktimeOperationKey('lan-http-fixture', {value: 1});
            const retry = window.inktimeOperationKey('lan-http-fixture', {value: 1});
            return [first, retry, window.inktimeOperationKey('lan-http-fixture', {value: 2})];
          } finally {
            window.inktimeOperationDone('lan-http-fixture');
            if (descriptor) Object.defineProperty(Crypto.prototype, 'randomUUID', descriptor);
            else delete Crypto.prototype.randomUUID;
          }
        }""")
        assert len(operation_keys[0]) == 36
        assert UUID(operation_keys[0]).version == 4
        assert operation_keys[0] == operation_keys[1]
        assert operation_keys[0] != operation_keys[2]
        page.evaluate("""() => {
          const originals = ['getItem', 'setItem', 'removeItem'].map(name => [name, Storage.prototype[name]]);
          originals.forEach(([name]) => Storage.prototype[name] = () => {throw new DOMException('blocked', 'SecurityError');});
          try {
            const first = window.inktimeOperationKey('storage-blocked-fixture', {value: 1});
            if (window.inktimeOperationKey('storage-blocked-fixture', {value: 1}) !== first) throw Error('retry changed operation');
            window.inktimeOperationDone('storage-blocked-fixture');
            if (window.inktimeOperationKey('storage-blocked-fixture', {value: 1}) === first) throw Error('completed operation reused');
          } finally {
            window.inktimeOperationDone('storage-blocked-fixture');
            originals.forEach(([name, method]) => Storage.prototype[name] = method);
          }
        }""")
        for label, path in (
            ("照片", "/photos"),
            ("工作", "/jobs"),
            ("模型", "/providers"),
            ("評分", "/scoring"),
            ("成本", "/costs"),
            ("模擬器", "/simulator"),
            ("渲染", "/rendering"),
            ("裝置", "/devices"),
            ("能源", "/energy"),
            ("維護", "/maintenance"),
            ("診斷", "/diagnostics"),
            ("設定", "/settings"),
        ):
            response = page.goto(base + path)
            assert response.status == 200, path
            assert page.locator("html").get_attribute("lang") == "zh-Hant-TW", label
            if artifact_dir and path in {"/photos", "/rendering", "/devices", "/settings"}:
                page.screenshot(path=str(Path(artifact_dir) / f"{path[1:]}.png"), full_page=True)
        page.goto(base + "/settings")
        expect(page.get_by_role("radio", name="進階", exact=True)).to_have_count(0)
        concurrency_card = page.locator('.setting-card[data-key="analysis.concurrency"]')
        concurrency = concurrency_card.locator('[name="analysis.concurrency"]')
        # All settings remain visible/searchable even while their dependency is
        # disabled. Enable AI before editing its concurrency; no model is called.
        expect(concurrency_card).to_be_visible()
        expect(concurrency).to_be_disabled()
        page.locator('[name="analysis.execution_mode"]').select_option("automatic_ai")
        expect(concurrency).to_be_enabled()
        page.locator('#settings-search').fill("AI 分析並行")
        expect(concurrency_card).to_be_visible()
        concurrency.fill("3")
        page.get_by_role("button", name="預覽影響").click()
        with page.expect_response(
            lambda response: response.url.endswith('/api/v1/settings') and response.request.method == 'POST'
        ) as saved:
            with page.expect_navigation(wait_until="domcontentloaded"):
                page.get_by_role("dialog").get_by_role("button", name="確認並儲存").click()
        assert saved.value.status == 200
        expect(concurrency).to_have_value("3")
        expect(page.locator('#dirty-count')).to_have_text("0")
        _review_draft_and_failure_regression(page, base)
        _crop_state_regression(page, base)
        _action_failure_regression(page, base)
        _virtual_display_failure_regression(page, base)
        _p3_filter_clear_regression(page, base)
        assert csp_violations == []
        assert javascript_errors == []
        browser.close()
