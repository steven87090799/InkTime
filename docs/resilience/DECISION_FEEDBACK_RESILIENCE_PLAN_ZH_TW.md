# InkTime 決策、韌性與發布安全實作計畫

此頁保留韌性功能的設計順序；現行主線已演進至 Migration 57，新增 AI Trace／Provider model／保留政策等功能，見[現行基線](../reference/CURRENT_STATE_ZH_TW.md)。Decision Trace、AI Trace 與 Job completed 分別代表選片、模型嘗試與工作狀態。


## 現有資料流與插入點

既有流程為 `RenderService.select_candidates_details()` 選出合格照片，`RenderService.publish()` 產生影像，`ReleaseCoordinator` 以 SQLite 交易寫入 Release、指派裝置與顯示歷史；裝置再以自動配對 Device Secret／version 或 Legacy Token 取得 Manifest、下載 payload、回報狀態。Migration、WAL 與跨程序 single-writer 已由 `Database.transaction()` 集中管理。

本功能在發布後以有界摘要寫入 Decision Trace；不改變既有選片、正式 Release、指派或 Display History。離線 Queue 與 Canary 另有資料表，只有明確 API／排程啟用後才寫入。

## 資料模型與索引

Migration 22 建立 algorithm version、decision trace／candidate、四種 feedback、Shadow 設定、device queue／event、retention policy／run／item、rollout campaign／stage／target／health／action；Migration 23 補上 correlation key 與資料一致性索引，Migration 24 補上分析／Vision Input 指紋。核心查詢均有 `(device_id, created_at)`、模式時間、Queue 狀態位置、Release、Rollout target 與 health event 索引；Trace 候選永遠只留前 50 筆。

## API、UI、排程與狀態

管理 API 位於 `/api/decision-traces`、`/api/feedback`、`/api/shadow`、`/api/devices/<id>/queue`、`/api/retention`、`/api/rollouts`；裝置 API 為 `/api/device/v1/queue/manifest` 與 canonical `/api/device/v1/queue/ack`。舊 `/api/device/queue/ack` 僅保留相容性。UI 使用既有管理站導覽中的「決策與韌性」六頁。

Queue 狀態為 `PENDING → READY → AVAILABLE → DOWNLOADED → ACKNOWLEDGED → DISPLAYED`，失敗、過期與取消是終止狀態。Rollout 轉換由 repository 集中限制：`DRAFT → VALIDATING → CANARY → OBSERVING/EXPANDING → COMPLETED`，任一步可暫停或進入 `ROLLING_BACK → ROLLED_BACK`。

## 失敗、保留與相容

Trace 寫入失敗不會回滾已成功的正式發布。裝置 ACK 需對應 credential、Queue 歸屬與 idempotency key；下載 URL 也驗證 Queue 歸屬與檔案雜湊。保留清理先記錄 dry run、分批處理，正式 Release、有效 Queue 與 Canary 引用是保護邊界。Migration 使用 `IF NOT EXISTS`、索引與既有 transaction/migration history，因此舊資料與未啟用行為保持不變。

## 分階段順序與驗證

1. Migration、algorithm version、Trace、audit 與 API error code。
2. Feedback 和評分調整查詢，影響分數但不改 AI 原分。
3. Shadow 設定與比較資料，永不寫正式顯示歷史。
4. Queue Manifest／ACK／Last Known Good。
5. Retention dry run 與批次清理。
6. Canary 狀態機、目標及 rollback audit。

驗證包含 migration、Trace 上限、權限／CSRF、跨裝置 Queue、ACK 冪等、retention dry run 與 rollout transition；效能測試以分頁與索引的 query plan 確認不全表 materialize。
