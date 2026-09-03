# 開發指南

模組責任：Route 驗證 HTTP；Service 管商業規則；Repository 管 SQL；Provider 管外部 API；Worker 管背景執行；domain 不依賴 Flask。禁止 Route 直接執行重型影像或模型呼叫。

先讀根目錄 [AGENTS.md](../../AGENTS.md) 與 [CI policy](../CI_POLICY.md)。一般開發的權威測試、建置、安全掃描、benchmark、瀏覽器與韌體編譯均在 GitHub Actions；不要把舊文件中的本機 pytest／Docker smoke／Playwright／soak 命令當作預設開發流程。

本機可做靜態檢查：

```bash
git diff --check
ruff check inktime tests scripts server.py analyze_photos.py
mypy inktime
```

推送後查一次相應 Actions；queued／in progress 回報 `CI_PENDING`，不以輪詢、重跑或手動 dispatch 製造通過。PR 維持 Draft；Ready、合併、auto-merge 需另有明確授權。CI 的 PR merge-ref 與 source HEAD 證據分開記錄。

Migration 只能新增版本，已發布版本不可改寫；目前為 1–52。測試 fixture 使用隔離資料與 Mock Provider，不依賴私人 NAS、真實 API 或家庭照片。錯誤需有穩定錯誤碼；Log 不得包含 Secret。未實作功能不可用空 UI 冒充完成。

修改功能時同步相關 README／Markdown；新增文件需更新[文件地圖](../README.md)與根目錄 `USER_MANUAL.html` 的索引。現行版本與模組入口見[基線](../reference/CURRENT_STATE_ZH_TW.md)及[架構](../architecture/ARCHITECTURE_ZH_TW.md)。硬體工作另須遵守 PhotoPainter 交接中的備份、GPIO 與實板證據界線。
