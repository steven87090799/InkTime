# Legacy 已退休

Legacy Web、Analyzer、Renderer 與其相容 runtime 已於 2026-08-27 從正式程式碼移除；InkTime 不再提供 Legacy runtime、Legacy routes、Legacy UI 或舊版離線腳本。

現行替代路徑：

- 照片檢視與 Review：`/photos` 與 `/photos/<id>`
- 電子紙模擬：`/simulator`
- 分析：Modern Web 建立工作，由 Worker 執行
- 渲染與發布：Modern Renderer、Release flow 與 Scheduler

既有 SQLite 中的 `photo_scores` 只作歷史資料保留。Retirement 沒有新增 Drop Migration、沒有刪除或改寫該表，也沒有回填或重新掃描照片；Modern runtime 不讀寫它。既有 Migration、Backup 與 Restore 相容行為維持不變。
