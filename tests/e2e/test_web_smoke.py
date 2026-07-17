from __future__ import annotations

import os

import pytest

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright


@pytest.mark.skipif(not os.environ.get("INKTIME_E2E_URL"), reason="只在 E2E 環境執行")
def test_first_setup_login_and_primary_console_pages():
    base = os.environ["INKTIME_E2E_URL"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(base + "/setup")
        page.get_by_label("管理員帳號").fill("e2e-admin")
        page.get_by_label("密碼（至少 12 個字元）").fill("e2e-password-long")
        page.get_by_label("再次輸入密碼").fill("e2e-password-long")
        page.get_by_role("button", name="建立並進入 InkTime").click()
        page.wait_for_url("**/dashboard")
        for label, path in (
            ("照片", "/photos"),
            ("工作", "/jobs"),
            ("模型", "/providers"),
            ("成本", "/costs"),
            ("渲染", "/rendering"),
            ("裝置", "/devices"),
            ("維護", "/maintenance"),
            ("診斷", "/diagnostics"),
            ("設定", "/settings"),
        ):
            page.goto(base + path)
            assert page.locator("html").get_attribute("lang") == "zh-Hant-TW", label
        page.goto(base + "/settings")
        page.locator('[name="analysis.concurrency"]').fill("3")
        page.on("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="儲存設定").click()
        browser.close()
