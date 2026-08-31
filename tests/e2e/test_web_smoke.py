from __future__ import annotations

import os

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright
expect = playwright_api.expect


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
        assert csp_violations == []
        browser.close()
