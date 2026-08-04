# InkTime Final One-Shot Hardening Audit

## Baseline

| 項目 | 值 |
|---|---|
| Audit baseline main | `064a599bd9e13dd95c6e7d326759d8efc9b22306` |
| Start HEAD | `064a599bd9e13dd95c6e7d326759d8efc9b22306` |
| Branch | `fix/final-production-openrouter-and-runtime-hardening` |
| Worktree | `/Users/steven/Desktop/inktime/InkTime-final-production-openrouter-runtime-hardening` |
| Baseline worktree | clean；主工作樹與其未追蹤／使用者檔案未觸碰 |
| Delivery terminal state | Draft PR、未 Merge、未 Mark Ready |

## 驗證政策

本次遵守施工手冊的本機限制：不執行 pytest、ruff、mypy、pip install/pip-audit、Docker/Compose、Playwright、Arduino/PlatformIO/ESP32 編譯、runtime soak、production smoke、真實 OpenRouter API 或付費呼叫。本機只做 Git、文字檔、差異與靜態契約整理；測試與安全掃描由 GitHub Actions 執行。

## 要求矩陣

| 區域 | 實作狀態 | 證據／後續驗證 |
|---|---|---|
| ESP32 TLS trust anchor、禁止 redirect、HTTP 明確 allowlist | PASS（GitHub exact-head） | CI `esp32-compile`；實體裝置 TLS 仍 NOT RUN |
| 配對密碼 Web + PhotoPainter 電子紙顯示 | PASS（GitHub exact-head） | CI firmware matrix；實體 BUSY/顯示仍 NOT RUN |
| Scanner presence 與 processing eligibility 分離 | PASS（GitHub exact-head） | Python unit/integration |
| Scheduler fault isolation 與 durable backup | PASS（GitHub exact-head） | Python unit/integration、Compose persistence |
| Provider allowlist、私有 HTTP、OpenRouter routing/reasoning/cache | PASS（GitHub exact-head） | Provider contract、mypy、官方文件對照 |
| 真實／預估／unknown 成本分離 | PASS（GitHub exact-head） | Python cost/usage contract |
| AI 512、單次 Vision、caption fingerprint、token cap、repair model | PASS（GitHub exact-head） | Python analysis contract、coverage |
| Offline benchmark、release schema version、Compose defaults、SBOM/Trivy | PASS（GitHub exact-head） | container-security + benchmark-contract + Compose |
| P3 version、redirect、secret registry、WAL | PASS（GitHub exact-head） | Python security/migration checks |
| 文件與逐項控制項說明 | PASS（靜態契約） | Markdown `63/63` 路徑已由 `USER_MANUAL.html` 連結 |

## Exact-head evidence

以下 hosted 證據對應本報告更新前的 implementation head `6dabcb5aefab995f3d81275e53946a6322cf4a50`；本次報告本身是文件-only 更新，推送後仍須以其新 head 的 GitHub required checks 作最後門檻。

| Workflow | Run | 結果 |
|---|---:|---|
| InkTime CI | `30863318865` | PASS；Python 3.10/3.12、ESP32、Compose TLS/LAN、Playwright、bounded soak、secret scan 全部 SUCCESS |
| InkTime Container Security and Benchmark Contracts | `30863318859` | PASS；benchmark contract 與 container security/SBOM/Trivy 全部 SUCCESS |

Python quality 的 hosted 摘要為 `1005 passed, 1 skipped`、coverage `80.04%`；benchmark harness 由明確 `--cov-config=pyproject.toml` 排除，並由獨立 offline benchmark contract 驗證。`pip-audit -r requirements.txt` 於 `cryptography==50.0.0` 上通過。`git diff --check` 通過；本機未執行施工手冊禁止的測試、建置或 runtime 操作。

## Real-environment boundary

本次 code/CI 可標記 `CODE_READY_REAL_ENV_PENDING`，但下列項目一律維持 `NOT RUN`，不得由 CI 推論為通過：OpenRouter 真實 API、付費呼叫與實際模型／價格行為；正式 NAS／反向代理；ESP32/PhotoPainter 實體 TLS 與配對畫面；功耗、deep sleep、GPIO5、BUSY、六色方向與 ghosting；真實生產資料及長時間現場 soak。

Draft PR 必須保持未 Merge、未 Mark Ready；本報告更新後新增的 exact-head required checks 仍須全部 SUCCESS 才能完成交付。
