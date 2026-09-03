# InkTime Application Factory

## 正式入口與初始化順序

正式 WSGI 入口保持 `server:app`，但 `server.py` 只呼叫
`inktime.app.factory.create_app()`；它只組裝 Modern Flask App。`factory.py` 被 import 時不建 App、不開 Database、不建立目錄，
只有明確呼叫 `create_app()` 才執行下列 Bootstrap：

1. 解析一次不可變 `RuntimeConfig`。
2. 建立獨立 Flask App，依 `proxy_trust` 套用 ProxyFix。
3. 在明確 Bootstrap 階段建立 Runtime 目錄、Database，執行同一條 Migration 安全路徑。
4. 由 `bootstrap_services(..., role="web")` 建立 process-local Repository 與 Service。
5. 設定 Session、CSRF、安全 Header、Auth 與錯誤處理。
6. 只載入 Modern Template／Static 並註冊 Modern Blueprints。
7. Health Detail 使用相同 RuntimeConfig 遮蔽摘要；Web 才執行 Release reconciliation。

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

Template loader 只指向 `inktime/app/web/templates`，Static 只使用 `/static/*`；應用程式不再註冊額外的相容頁面或資產路徑。

Auth、CSRF 與 HTTP error handler 只由 Modern Root 註冊。Device Bearer API 保持 CSRF
豁免；HTML／JSON 錯誤行為由 Modern Root 統一處理。

## Migration、測試與 Rollback

三個 process role 都呼叫既有 `migrate()`；Docker 仍讓 Worker／Scheduler 等待 Web
readiness，Migration lock、交易 rollback、pre-migration backup 與 integrity check 不變。
Factory 分離本身不新增 Schema；整個主線目前有 Migration 1–57，不能據此略過其他功能的升級。

回滾必須停止三個 process，確認舊程式可讀目前 Schema；若跨越不相容 Migration，先依[備份還原指南](../operations/BACKUP_RESTORE_ZH_TW.md)還原相容快照，再啟動相容映像。Factory 分離的歷史無 Schema 變更，不代表之後所有版本都能只回退程式。
