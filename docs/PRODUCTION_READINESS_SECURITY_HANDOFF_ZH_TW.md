# Security／Production Readiness 最終交接

本文件記錄 PR #53 安全強化分支最後一輪的操作契約與人工邊界。自動化測試通過不代表真實 NAS、正式憑證、OpenRouter 或電子紙硬體已驗證；未實際執行的項目必須標記 `NOT RUN`。Hosted provenance 必須區分 `PR_HEAD`、`TESTED_MERGE_REF` 與 `EXACT_HEAD_WORKFLOW_RUN`。

## 本輪 One-shot hardening 範圍

- 基準為最新 `origin/main`；正式 schema source 目前為 `Migration 33`。Migration 32 保存 Provider options/capabilities、usage 的 cache-write、成本來源與 request-size metrics；Migration 33 增加 Provider identity、OpenRouter legacy data fix 與成本回溯索引，舊 Migration 1–31 不修改。
- Provider 路徑新增正式 OpenRouter contract、受控 routing/privacy options、reasoning／session routing 與 Batch hard guard；Vision 與 text-only JSON repair 共用 policy helper；成本來源分為 `provider_reported`、`estimated`、`unknown`，unknown 不當作零成本。
- AI 請求固定 512／1024／1600 image side；完整、變體、文字修復分別受 2048／3072／1200 token cap 約束。repair policy 在 Analysis Plan 建立時 freeze，但不進 Vision fingerprint；每個 job 最多一次 repair，且 repair 不重新上傳圖片。
- ESP32 backend transport 只接受有 trust anchor 的 HTTPS；HTTP 僅限明確的私有 LAN 開發設定，沒有 `setInsecure()` fallback。首次配網會在 AP 頁面與裝置畫面顯示一次性的隨機 AP 密碼。
- production Compose 預設 loopback bind、HTTPS public URL、Secure cookie 與禁止 insecure HTTP；`docker-compose.dev.yml` 才提供明確的本機開發覆寫。
- Provider Level 1/2/3 只由管理員明確按鈕觸發；Level 2/3 使用 synthetic image 並有 request／cost 邊界。離線 benchmark 預設不呼叫外部 Provider、不寫 production analysis/release/history/cache；quality／ranking metrics 與 contract metrics 分開，Container workflow 另以 Syft 產生 SBOM、Trivy 掃描 High/Critical，結果由 final-head GitHub Actions 決定。

詳細的 Provider、benchmark 與韌體 trust-anchor 操作分別見
[OpenRouter 正式 Provider 與安全契約](providers/OPENROUTER_ZH_TW.md)、
[Model Benchmark 規格](providers/MODEL_BENCHMARK_ZH_TW.md) 與
[ESP32 TLS／配網信任根配置](devices/ESP32_TLS_PROVISIONING_ZH_TW.md)。

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

ESP32 status firmware 以 ArduinoJson 寫入真正 number／Boolean；沒有把數值或布林序列化成字串。韌體 2.5.0 已實作 Queue-first Manifest、strict Item download、NVS-persisted canonical `/api/device/v1/queue/ack`、穩定 idempotency key、409 stale handling、bounded retry 與 verified same-content skip；Server 契約保持嚴格，沒有加入無聲 legacy coercion。

## Device Token／共享 IP

單一 `BEGIN IMMEDIATE` 內先以安全 hash 查詢 enabled Token。有效 Token 立即更新 last-seen 並通過，不查 shared-IP failure gate，也不刪除該 IP 的攻擊紀錄。只有 invalid／revoked／disabled Token 才清理過期列、查 gate、寫入 attempt；最近五分鐘上限 20，table 額外限制 10000 列，`Retry-After` 限制 1–300 秒。Failure table 只保存 keyed IP hash，任何表都不保存 plaintext Token；既有 `devices.last_ip` 診斷欄位行為不變。

## Webhook delivery

Webhook 採 at-least-once。每個事件持久化穩定 Event ID，所有後續 retry 重用 `Idempotency-Key`、`X-InkTime-Event-ID` 與 payload `event_id`。只在 TCP／TLS connect 尚未完成且 request 尚未開始時切換下一個 pinned IP；一旦 request 可能已部分或全部送出，timeout、reset、broken pipe、remote disconnect 或 HTTP parse error 都不會在同一次 delivery 中換 IP。接收端必須依 Key 去重。

## Production gates

- `compose-production-smoke`：保留明確 HTTP break-glass 測試，不當作 TLS 證據。
- `compose-lan-production-persistence`：使用專用 production LAN env、絕對 Volume、degraded transport diagnostics、登入／CSRF／Device Token／Queue download／ACK、Compose restart、down/up 同一儲存、離線 integrity／Migration 27、備份、破壞後還原與還原後 API 驗證。CI runner／本機 Docker 證據不等於真實 NAS reboot／filesystem／ACL。
- `compose-production-tls-smoke`：用一次性測試 CA、SAN certificate、Nginx 與不屬保留 suffix 的 `inktime-ci.acme.dev`；client 明確信任 CA，不使用 `verify=False`／ignore-certificate。驗證 HTTP redirect、TLS hostname/chain、Secure＋HttpOnly＋SameSite=Strict、CSRF、login/logout/dashboard、HTTPS-only HSTS、production preflight 與 proxy hop diagnostics；backend port 不公開。
- `bounded-runtime-soak`：Web app、Worker、Scheduler 同時執行；重複 session、device auth success/failure、Queue manifest/ACK、release metadata、scan、scheduler heartbeat 與 webhook mock。輸出 RSS、thread、FD、SQLite connection／writer、open file、child process、pending async work／job、oldest job、scheduler age、WAL、timeout、cleanup、exit status 與 final JSON summary。手動 workflow 可跑 30 分鐘、2 小時或 5 小時；24 小時只在受控 LAN 主機本地執行。
- Backup/Restore：fresh database → full metadata backup → fresh target restore；驗證 Migration 27、administrator/password/session、device token、release/queue、settings、Batch lifecycle tables、Review／offline schedule tables、Secret exclusion、Worker/Scheduler bootstrap。舊 snapshot upgrade 由 migration fixtures 覆蓋。
- Current schema gate：Release image 與 LAN production gate 都從 `inktime/app/db/migrations.py` 讀取目前最高 Migration，不再各自維護硬編碼版本；本輪預期為 Migration 33。Migration 32／33 的 upgrade／fresh／integrity／rollback 證據必須在 Final-Head CI 取得。
- Container supply-chain：`container-security.yml` 在 exact checkout 建置 image，輸出 CycloneDX SBOM，並以 Trivy 掃描 High/Critical；只有 `.trivyignore` 內逐一列出、含 owner／reason／expiry 的暫時 unfixed CVE 例外不阻擋，未列入的 High/Critical 仍使 workflow 失敗。例外到期前必須重評估 pinned base image；此 workflow 不代表真實 NAS host、registry 或 production image 已驗證。
- Rollback：不支援只降程式、不還原 DB。必須停止 Web/Worker/Scheduler、還原相容 snapshot，再切回相容 image/commit。

## Dependency／Actions

- 2026-07-29 GitHub push banner 與 alert #1 確認 `pytest` direct development dependency 受 GHSA-6w46-j5rx-g56g／CVE-2025-71176 影響（`<9.0.3`，UNIX tmpdir local privilege／DoS，runtime 不載入）；已將 `requirements-dev.txt` 升至首個修正版 9.0.3。`pip-audit -r requirements.txt` 的 runtime dependency 掃描無已知漏洞，最終仍以 Final-Head CI 為準。
- `actions/checkout` v7.0.1、`actions/setup-python` v7.0.0、`gitleaks-action` v3.0.0 已更新至官方 Node 24 版本並 pin commit SHA。
- `arduino/setup-arduino-cli` 官方最新 v2.0.0 仍宣告 `node20`；保留官方 commit `81d310742121c928ea9c8bbd407b4217b432ae02`。移除條件：官方發布 Node 24 相容正式版並通過完整 ESP32 compile matrix。

## OpenAI Batch 邊界

Batch 已完成 Worker-managed 的持久化生命週期：選片、JSONL 分片、提交、poll、結果對帳、冪等匯入、失敗重試、成本、重啟恢復與遠端檔案清理。正式啟用前仍須由管理員完成 Provider／價格設定與 100 張 Sample 驗收；真實 OpenAI 與 NAS／硬體驗證不由 CI 代替。

OpenRouter 不進入這條 Batch 路徑；generic OpenAI-compatible Provider 只有在管理員確認 `/files`、`/batches`、結果／錯誤檔下載與刪除契約後才可勾選 Batch。模型離線比較使用 bounded benchmark；live benchmark 不是 CI 預設，也不得使用 production DB 或 production AI cache。

## 人工驗證

以下不能以 simulator、compile、CI 或測試 CA取代：正式 DNS／certificate chain／NAS restart persistence；GDEY、GDEP、PhotoPainter 實機下載與 Queue ACK；withdraw 後裝置行為、BUSY timing、GPIO5、deep sleep/wake、六色方向、殘影、斷網／失敗恢復與整板功耗。未執行時一律寫 `NOT RUN`。

交接狀態：

- Automated software validation: PASS（仍以 Draft PR final-Head CI 終態為最終依據）
- Physical hardware validation: NOT RUN
- Real NAS validation: NOT RUN
- Public DNS／certificate validation: NOT RUN（不在本次範圍）
