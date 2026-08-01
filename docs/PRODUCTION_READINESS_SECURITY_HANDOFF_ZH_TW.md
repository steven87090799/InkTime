# Security／Production Readiness 最終交接

本文件記錄安全強化分支最後一輪的操作契約與人工邊界。自動化測試通過不代表真實 NAS、正式憑證或電子紙硬體已驗證；未實際執行的項目必須標記 `NOT RUN`。

## Queue Manifest 與 Release

- `ResilienceRepository` 只查 Queue／Queue Item 資料，不讀取 Release filesystem。
- `DeviceQueueManifestService` 對每個 active、未逾期 Queue Item 呼叫 `DeviceReleaseService.authorize_release_for_device()`；只有 `allowed=true` 才可廣告。
- Manifest 與下載共用同一個 payload entry validator：單一相對 `.bin` 檔名、1–64 MiB、真正 JSON integer size、64 位 SHA-256；拒絕 slash、backslash、`..`、NUL、`manifest.json`、symlink 與被替換的 release directory。
- missing／staged／staged_failed／withdrawn／deleted release、wrong profile、其他裝置、cancelled／expired item 一律不出現在 response；GET 不更改 Queue Item 狀態。被略過項目只寫入不含路徑與 Secret 的 bounded audit event。

## Strict JSON Inventory

所有 `inktime/app/api` JSON 入口共用 bounded object loader。Malformed JSON 或 top-level array／scalar／Boolean／null 回 400；非 JSON Content-Type 回 415；超過 endpoint 上限回 413。Upload/form/query string 使用自己的 parser，不受 JSON body limit 或 JSON scalar 規則誤傷。

| Endpoint | Field | 先前行為 | 最終型別／範圍 | 錯誤 |
|---|---|---|---|---|
| `POST /api/v1/jobs` | `limit` | `int()`＋clamp | JSON integer，1–100000，可省略／null 不允許 | 400 |
| 同上 | `budget_limit` | `float()` | JSON number 或 null，finite，0–1000000 | 400 |
| 同上 | `force_recompute` | 部分 helper | JSON Boolean | 400 |
| `POST /api/v1/jobs/selection-preview` | `limit` | `int()`＋clamp | JSON integer，1–100000，可省略 | 400 |
| `POST /api/v1/jobs/estimate` | `photo_count` | `int()`＋clamp | JSON integer，0–1000000 | 400 |
| `PATCH /api/v1/schedules/{key}` | timeout／retry fields | `_bounded(int(value))` | JSON integer；timeout 30–86400、retry 0–10、interval 30–86400 | 400 |
| 同上 | `weekdays` | `isinstance(day,int)` | JSON integer array，每項 0–6，Boolean 不算 integer | 400 |
| 同上 | nested `config` | kind-specific parser 仍可 coercion | kind-specific schema；integer/Boolean 精確型別與各欄位範圍 | 400 |
| `POST /api/v1/maintenance/cache/{estimate,cleanup}` | `max_bytes`／`retention_days` | `int()` | JSON integer；1–10 TiB／0–3650 days | 400 |
| `PATCH /api/v1/devices/{id}/energy-profile` | battery/current/refresh/reserve | `float()` 接受字串 | JSON number，finite；各欄位依能源 domain 範圍，可 nullable 欄位只接受 null | 400 |
| `POST /api/device/v1/status` | telemetry integer／float／Boolean | 部分 strict | JSON integer／finite number／Boolean；沿用既有 telemetry 範圍，64 KiB | 400／413 |
| `POST/PATCH /api/feedback` | `days`／`value` | `int()`／`float()` | days JSON integer 1–3650；value finite number -1–1 | 400 |
| `POST /api/devices/{id}/queue/generate` | `depth`／`priority` | `int()`／clamp | JSON integer；1–14／1–1000 | 400 |
| `POST /api/device/v1/queue/ack` | `queue_version` | `int(str())` | JSON integer，0–2147483647，16 KiB | 400／413 |
| `POST /api/v1/providers` | priority/concurrency/timeouts/quotas | `int()`；UI quota 送字串 | JSON integer；priority 1–10000、concurrency 1–32、timeout 5–600、cooldown 1–86400、quota 1–2147483647 或 null | 400 |
| `POST /api/v1/scoring/profiles` | weights／bonus | `float()` | finite JSON number；weight 0–100、bonus -100–100 | 400 |

ESP32 status firmware 以 ArduinoJson 寫入真正 number／Boolean；沒有把數值或布林序列化成字串。現行韌體尚未實作 Queue ACK client，因此 Server 契約保持嚴格，沒有加入無聲 legacy coercion。

## Device Token／共享 IP

單一 `BEGIN IMMEDIATE` 內先以安全 hash 查詢 enabled Token。有效 Token 立即更新 last-seen 並通過，不查 shared-IP failure gate，也不刪除該 IP 的攻擊紀錄。只有 invalid／revoked／disabled Token 才清理過期列、查 gate、寫入 attempt；最近五分鐘上限 20，table 額外限制 10000 列，`Retry-After` 限制 1–300 秒。Failure table 只保存 keyed IP hash，任何表都不保存 plaintext Token；既有 `devices.last_ip` 診斷欄位行為不變。

## Webhook delivery

Webhook 採 at-least-once。每個事件持久化穩定 Event ID，所有後續 retry 重用 `Idempotency-Key`、`X-InkTime-Event-ID` 與 payload `event_id`。只在 TCP／TLS connect 尚未完成且 request 尚未開始時切換下一個 pinned IP；一旦 request 可能已部分或全部送出，timeout、reset、broken pipe、remote disconnect 或 HTTP parse error 都不會在同一次 delivery 中換 IP。接收端必須依 Key 去重。

## Production gates

- `compose-production-smoke`：保留明確 HTTP break-glass 測試，不當作 TLS 證據。
- `compose-production-tls-smoke`：用一次性測試 CA、SAN certificate、Nginx 與不屬保留 suffix 的 `inktime-ci.acme.dev`；client 明確信任 CA，不使用 `verify=False`／ignore-certificate。驗證 HTTP redirect、TLS hostname/chain、Secure＋HttpOnly＋SameSite=Strict、CSRF、login/logout/dashboard、HTTPS-only HSTS、production preflight 與 proxy hop diagnostics；backend port 不公開。
- `bounded-runtime-soak`：Web app、Worker、Scheduler 同時執行；重複 session、device auth success/failure、Queue manifest/ACK、release metadata、scan、scheduler heartbeat 與 webhook mock。輸出 RSS、Python heap、thread、FD、SQLite writer、open DB、child process、pending job、stuck lease、WAL、timeout、cleanup、exit status 與 final summary。
- Backup/Restore：fresh database → full metadata backup → fresh target restore；驗證 Migration 25、administrator/password/session、device token、release/queue、settings、Secret exclusion、Worker/Scheduler bootstrap。舊 snapshot upgrade 由 migration fixtures 覆蓋。
- Rollback：不支援只降程式、不還原 DB。必須停止 Web/Worker/Scheduler、還原相容 snapshot，再切回相容 image/commit。

## Dependency／Actions

- 2026-07-29 GitHub push banner 與 alert #1 確認 `pytest` direct development dependency 受 GHSA-6w46-j5rx-g56g／CVE-2025-71176 影響（`<9.0.3`，UNIX tmpdir local privilege／DoS，runtime 不載入）；已將 `requirements-dev.txt` 升至首個修正版 9.0.3。`pip-audit -r requirements.txt` 的 runtime dependency 掃描無已知漏洞，最終仍以 Final-Head CI 為準。
- `actions/checkout` v7.0.1、`actions/setup-python` v7.0.0、`gitleaks-action` v3.0.0 已更新至官方 Node 24 版本並 pin commit SHA。
- `arduino/setup-arduino-cli` 官方最新 v2.0.0 仍宣告 `node20`；保留官方 commit `81d310742121c928ea9c8bbd407b4217b432ae02`。移除條件：官方發布 Node 24 相容正式版並通過完整 ESP32 compile matrix。

## OpenAI Batch 邊界

Batch 仍是 `Experimental／Provider API only`，UI 不預設勾選，文件要求保持關閉。現況只有 submit/query/cancel primitives，沒有 persistent lifecycle、restart recovery、result ingestion、retry、cost accounting 或 cancellation compensation；不得描述為 Worker-managed production job。

## 人工驗證

以下不能以 simulator、compile、CI 或測試 CA取代：正式 DNS／certificate chain／NAS restart persistence；GDEY、GDEP、PhotoPainter 實機下載與 Queue ACK；withdraw 後裝置行為、BUSY timing、GPIO5、deep sleep/wake、六色方向、殘影、斷網／失敗恢復與整板功耗。未執行時一律寫 `NOT RUN`。
