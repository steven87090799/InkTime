# InkTime Application Factory

## 正式入口與初始化順序

正式 WSGI 入口保持 `server:app`，但 `server.py` 只呼叫
`inktime.app.factory.create_app()`；它不再 import `legacy_server` 或借用任何
Legacy Flask App。`factory.py` 被 import 時不建 App、不開 Database、不建立目錄，
只有明確呼叫 `create_app()` 才執行下列 Bootstrap：

1. 解析一次不可變 `RuntimeConfig`。
2. 建立獨立 Flask App，依 `proxy_trust` 套用 ProxyFix。
3. 在明確 Bootstrap 階段建立 Runtime 目錄、Database，執行同一條 Migration 安全路徑。
4. 由 `bootstrap_services(..., role="web")` 建立 process-local Repository 與 Service。
5. 設定 Session、CSRF、安全 Header、Auth 與錯誤處理。
6. 只載入 Modern Template／Static 並註冊 Modern Blueprints。
7. `legacy_enabled=true` 時才 lazy import、驗證資產碰撞並註冊 `/legacy/*`。
8. Health Detail 使用相同 RuntimeConfig 遮蔽摘要；Web 才執行 Release reconciliation。

`configure_web_application()` 對同一 App 的第二次初始化會明確失敗，避免重複
Migration、Blueprint 或 Extension。每次 `create_app()` 都會產生不同 Database、
Cache、Extension 與 Service instance；測試直接傳入 Temporary `RuntimeConfig`，不繞過
正式初始化契約。

## Web、Worker、Scheduler

三個入口都使用 `resolve_runtime_config()` 與 `bootstrap_services()`，但 role 不同：

| Process | Bootstrap | 不載入內容 |
|---|---|---|
| Web | 完整 Repository／Service、Auth、Template、Blueprint、Health | Worker／Scheduler thread |
| Worker | 工作、照片、Provider、Renderer、Release、Backup 所需 Service | Flask、Template、Blueprint、Auth Repository |
| Scheduler | Schedule、Job、Notification、Backup、Observability | Flask、Template、Renderer、Provider、照片分析 |

Worker、Scheduler 與 `analyze_photos.py` 不 import `server:app`，也不建立完整 Web App。
任何入口錯誤都只顯示型別化／已遮蔽的診斷，不輸出 Token 或 Credential。

## Template、Static 與錯誤處理

Modern loader 只指向 `inktime/app/web/templates`；Legacy Blueprint 只指向
`inktime/app/legacy/templates`，且所有 Legacy Template 使用 `legacy/` namespace。
Static 分別使用 `/static/*` 與 `/legacy/static/*`。註冊 Legacy 前會比較兩側相對檔名，
任何 Template／Static collision 都明確中止 Legacy 註冊，不依賴 loader 順序。

Auth、CSRF 與 HTTP error handler 只由 Modern Root 註冊。Device Bearer API 保持 CSRF
豁免；Legacy 不註冊 error handler，因此不能覆蓋 Modern HTML／JSON 行為。

## Migration、測試與 Rollback

三個 process role 都呼叫既有 `migrate()`；Docker 仍讓 Worker／Scheduler 等待 Web
readiness。Migration 50 是目前最高版本；Migration lock、交易 rollback、pre-migration
backup、`foreign_key_check`／`integrity_check` 與未完成 migration history 的 fail-closed
recovery 契約由同一路徑執行。若資料庫含程式不認得的較新 Schema，必須停止啟動，
不能由舊映像降級寫入。

回滾方式是停止三個 process，以升級前 SQLite backup 搭配與該 Schema 相容的舊映像
離線恢復，再啟動三服務。不可只切回程式 commit 卻沿用較新的正式資料庫；專案不提供
Down Migration。Release 目錄與 Metadata DB 的一致性由啟動 reconciliation 驗證。
