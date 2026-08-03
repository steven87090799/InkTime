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
| ESP32 TLS trust anchor、禁止 redirect、HTTP 明確 allowlist | pending | GitHub firmware contract/compile；實體裝置仍 NOT RUN |
| 配對密碼 Web + PhotoPainter 電子紙顯示 | pending | GitHub firmware contract；實體 BUSY/顯示仍 NOT RUN |
| Scanner presence 與 processing eligibility 分離 | pending | GitHub unit/integration |
| Scheduler fault isolation 與 durable backup | pending | GitHub unit/integration、Compose persistence |
| Provider allowlist、私有 HTTP、OpenRouter routing/reasoning/cache | in progress | Provider contract tests + current official docs |
| 真實／預估／unknown 成本分離 | pending | GitHub cost contract |
| AI 512、單次 Vision、caption fingerprint、token cap、repair model | pending | GitHub analysis contract |
| Offline benchmark、release schema version、Compose defaults、SBOM/Trivy | pending | GitHub jobs；NAS/生產環境仍 NOT RUN |
| P3 version、redirect、secret registry、WAL | in progress | GitHub security/migration checks |
| 文件與逐項控制項說明 | pending | Markdown link/contract checks |

## 終局判定規則

只有新分支推送後的 exact-head GitHub Actions 全部通過，且 Draft PR 仍未 Merge、未 Mark Ready，才可標記 `CODE_READY_REAL_ENV_PENDING`。OpenRouter 真實 API、正式 NAS/反向代理、ESP32/PhotoPainter 實體 TLS、功耗、BUSY、六色方向與 ghosting 驗證在本報告中一律維持 `NOT RUN`，不得由 CI 推論為通過。
