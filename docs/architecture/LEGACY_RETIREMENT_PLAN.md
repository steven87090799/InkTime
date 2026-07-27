# Legacy Retirement Plan

## 相容功能與替代路徑

Legacy 預設關閉，只有 `RuntimeConfig.legacy_enabled=true` 才 lazy import
`inktime.app.legacy.blueprint`。關閉時不 import `legacy_server`、
`legacy_analyze_photos`、`render_daily_photo*`、AppleScript 或 simulator-only dependency。

| Legacy route／功能 | Modern 替代 | 保留原因 | Removal criteria |
|---|---|---|---|
| `/legacy/review` | `/photos`、`/photos/<id>` | 短期操作習慣相容 | 管理員完成 Modern review 驗收且相容期結束 |
| `/legacy/sim` | `/simulator` | 短期預覽入口相容 | Modern simulator 覆蓋既有維護案例 |
| `/legacy/sim-render` | Modern preview API | Legacy 頁面只讀圖片顯示 | `/legacy/sim` 可移除時一併移除 |
| `/legacy/api/md-list` | Modern materialized capture-date query | 舊頁日期選擇器 | 舊頁移除 |
| 舊 URL-key Device API | Bearer `/api/device/v1/*` | 僅留原始碼供緊急人工回查 | 不再註冊；確認無舊韌體使用後刪碼 |

所有已註冊 Legacy route 位於 `/legacy`、Administrator Only，沒有 Root、全域 error
handler 或寫入 route。若未來不得已加入維護寫入，必須沿用 Modern global CSRF，且不得
擴充成新產品功能。每頁固定顯示 Deprecated banner、Modern 替代路徑、移除條件與
「禁止新增 Legacy 功能」。

## `photo_scores` Reader／Writer 盤點與單向邊界

正式權威來源是 `photos`、最新 `photo_analysis`、`photo_events` 與既有 Modern
Repository。Modern Web、Worker、Scheduler、CLI、Device API、Renderer 與 Release flow
不讀、不寫 `photo_scores`。

仍存在但不在正式啟動圖中的舊碼：

- `legacy_analyze_photos.py`：建立／補欄、查詢與寫入 `photo_scores` 的離線舊 Analyzer。
- `legacy_server.py`：舊 Review／Simulator 與 metadata backfill，已不被 `server.py` import。
- `render_daily_photo.py`、`render_daily_photo_133c.py`：唯讀舊表的離線 Renderer。
- `scripts/daily_render.sh`：僅包裝上述離線 Renderer 的舊範例，不在 Docker／Gunicorn／新版 Scheduler 啟動圖中。
- Migration／Backup tests：只驗證舊表不會被 Migration 刪除。

新 `LegacyPhotoRepositoryAdapter` 只包裝 `PhotoRepository`：回傳 frozen DTO、不暴露
`sqlite3.Row`、每頁最多 100 筆、offset 最多 100,000、日期只查
`photos.captured_month_day` 索引，不掃全庫 EXIF JSON。圖片以 Photo ID 經 Modern
Repository 與 path containment 解析。Adapter 沒有 insert／update／delete；測試另外用
SQLite trigger 將 `photo_scores` 設成硬性唯讀，證明 Adapter 不觸碰舊表。

本階段不做資料搬移、不新增 Migration、不讀原圖做 backfill、不呼叫 Provider、不批次
重新分析，也不建立任何雙向同步。`photo_scores` 保留原狀供離線回滾；新功能禁止新增
欄位或 Writer。

## AppleScript、Lock 與移除門檻

Core／Factory／Bootstrap 不 import AppleScript。`osascript` 只留在 Darwin-only
`legacy_analyze_photos.py` maintenance adapter；Linux、Windows 與 Docker 不會呼叫。
Writer、Runtime、Migration 與 Thumbnail lock 繼續使用 PR #27/#28 的 `fcntl`／SQLite
single-writer 行為；共用 `FcntlLockProvider` 提供一致 timeout／錯誤與 Fake 可注入邊界。
Windows Native 會明確要求改用 Linux Docker，Windows 上的 Linux Container 正常。

Legacy 原始碼可刪除前必須同時證明：Modern review/simulator 完成操作驗收、無舊 URL-key
韌體、`photo_scores` 不再有人工離線讀取需求、至少一個完整備份保留且 restore/integrity
通過。回滾只需關閉 Legacy flag或回復程式 commit；不刪表、不執行 Down Migration。
