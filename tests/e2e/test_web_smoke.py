from __future__ import annotations

import os
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


@pytest.mark.skipif(not os.environ.get("INKTIME_E2E_URL"), reason="只在 E2E 環境執行")
def test_first_setup_login_and_primary_console_pages():
    base = os.environ["INKTIME_E2E_URL"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        csp_violations: list[str] = []
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
            page.goto(base + path)
            assert page.locator("html").get_attribute("lang") == "zh-Hant-TW", label
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
        assert csp_violations == []
        browser.close()
