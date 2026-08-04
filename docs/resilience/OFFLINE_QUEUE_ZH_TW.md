# 裝置離線內容佇列

管理員以 `POST /api/devices/<id>/queue/generate` 建立深度 1–14 的 Queue，並加入已發布 Release。裝置使用自動配對 Device Secret／version 或 Legacy Token 讀取 `GET /api/device/v1/queue/manifest`，再以 Queue Item 專屬 URL 下載；URL、Queue 歸屬、Release Manifest 與 SHA-256 都會驗證。

`online_queue` 只服務相容的 Online 裝置；Enhanced `inktime_offline_schedule` 裝置在中央 enqueue、資料庫 guard、Canary targeting 與 rollback 都會被拒絕或明確 skip，不會收到 Firmware 永遠不會讀取的 generic Queue Item。Offline Slot 只能由 `prepare_day()` 以 target date、config version 與 schedule snapshot 原子建立；今日與未來日期各自擁有 Queue Item，重試今日不會取消明日。

裝置 delivery invariant 是資料庫與 Repository 的共同契約：Enhanced 必須 `offline_prefetch_allowed=1`，`legacy_online`／`stock_compat` 必須是 `0`。Migration 31 先修復舊矛盾資料，再以 devices INSERT／UPDATE trigger 拒絕新矛盾；API 省略欄位時依 delivery mode 正規化，明確矛盾回 `400 DEVICE-008`。Scheduler 的 due query 只接受 enabled Enhanced + prefetch=1。

一般 Online 韌體先讀取 Queue；只有 Queue endpoint 回 404 或 Queue 為空時，才回退既有 Latest Release。Manifest 必須是 bounded JSON object，Queue version、尺寸與大小必須是真正 JSON integer；下載 URL 必須是 Item 綁定的同源相對路徑。Content-Type、Content-Length、實際長度、Profile、尺寸與 SHA-256 任一不符都會 fail closed，保留舊畫面。

裝置以 canonical `POST /api/device/v1/queue/ack` 回報 Manifest、下載、雜湊與顯示事件。每個事件先把穩定 idempotency key 與 payload 寫入 NVS；只有 HTTP 2xx 才清除，timeout／5xx／重新開機會重送同一 Key，409 視為 stale Queue version。401／403 不會降級成匿名或 Latest 成功。只有 `DISPLAY_COMPLETED` 才更新 current／Last Known Good；指標依實際 `displayed_at` 時間排序，晚到的舊 ACK 仍寫歷史但不得把 current/LKG 倒退；相同時間綁定不同 Release 會 fail closed。下載成功不等於已顯示。舊 `/api/device/queue/ack` 僅保留相容路由，不應用於新韌體或新文件。

Enhanced `local_next` 是人工 cache-only 預覽；NVS cursor 讓每次按鍵依 active schedule 前進並 wrap，schedule id 改變時 reset，重複 SHA 會跳過。它不連 Wi-Fi、不發 terminal ACK／journal、不改 queue status 或 server current/LKG；正式 timer wake 不受 cursor 影響，即使同 SHA 只略過物理刷新，仍回報正式 terminal event。

`schedule_not_ready` 的 `retry_after_epoch` 不得越過今日下一個 `next_slot_epoch`；今日尚有未來 Slot 時，重試留在今日，只有今日所有 Slot 已過才允許明日 prepare point。若本地 20:00 後今日沒有 `slot.show_at > local_now`，Scheduler 只建立 tomorrow job；今日仍有未來 Slot 才可與 tomorrow 一起準備。

成功顯示後，韌體在 NVS 保存 SHA-256、Release、render profile、rotation、board profile 與成功標記。下一次只有所有欄位完全相同且狀態完整時才回報 `display_skipped=true` 並跳過面板刷新；forced refresh、rotation／profile／board 改變或 NVS 損壞都不可 skip。

## 跨日生命週期

`GET /api/device/v1/offline-schedule` 只接受 `target=current`（預設）與 `target=next`。current 200 另提供 `next_target_start_epoch`、`next_schedule_prefetch_epoch`；所有 epoch 由裝置 IANA 時區在 server 計算，韌體不以固定 offset 推算明日。

`target=next` 的 snapshot 會先以 `.tmp` 寫入 staged-next、驗證完整 SHA／Queue identity／Profile／rotation／config version／Slot 範圍，再 rename 成 staged file。active schedule 在此期間保持不變。RTC 到達 target start 後，裝置再次驗證 `target_start == active.target_end` 與下一個本地日，才以 `.bak` rollback-safe 的原子 promote，最後套用 future config，使 00:00 formal Slot 可服務。

明日 prefetch 的 non-terminal ACK 只能到 `HASH_VERIFIED`；在真正刷新前不產生 display terminal event，Queue 保持 `ACKNOWLEDGED`。`local_next` 成功不清除 retry；只有 formal display、formal schedule download 或 promotion 成功才清除。Scheduler 對 today 與 tomorrow 分開決策，避免已到明日技術截止時捨棄 today 尚未到的晚間 Slot。
