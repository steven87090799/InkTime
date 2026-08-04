# InkTime Runtime Configuration

`inktime.app.core.runtime_config.RuntimeConfig` 是 Web、Worker、Scheduler 與 CLI 共用的
不可變部署設定。固定優先順序為「明確函式參數 → Environment → 安全部署預設」；
SQLite `settings` 的動態業務設定不參與這個優先序。

| 欄位 | 型別 | Environment | 安全預設 | 動態修改 | Secret |
|---|---|---|---|---|---|
| `environment` | `str` | `INKTIME_ENVIRONMENT` | `development` | 否 | 否 |
| `data_dir` | `Path` | `INKTIME_DATA_DIR` | 開發為 repo `data`；production 為 `/data` | 否 | 否 |
| `database_path` | `Path` | `INKTIME_DATABASE` | `<data_dir>/inktime.db` | 否 | 否 |
| `photo_dir` | `Path` | `INKTIME_PHOTO_DIR` | 開發為 `simulation_photos`；production 為 `/photos` | 否 | 否 |
| `release_dir` | `Path` | `INKTIME_RELEASE_DIR` | `<data_dir>/releases` | 否 | 否 |
| `backup_dir` | `Path` | `INKTIME_BACKUP_DIR` | `<data_dir>/backups` | 否 | 否 |
| `cache_dir` | `Path` | `INKTIME_CACHE_DIR` | `<data_dir>/cache` | 否 | 否 |
| `host` | `str` | `INKTIME_HOST` | `127.0.0.1` | 否 | 否 |
| `port` | `int` | `INKTIME_PORT` | `8765` | 否 | 否 |
| `timezone` | IANA `str` | `INKTIME_TIMEZONE`／`TZ` | `Asia/Taipei` | 否 | 否 |
| `proxy_trust` | `int` | `INKTIME_PROXY_TRUST` | `0` | 否 | 否 |
| `legacy_enabled` | `bool` | `INKTIME_ENABLE_LEGACY_WEBUI` | `false` | 否 | 否 |
| `development` | `bool` | `INKTIME_DEVELOPMENT` | 依 environment | 否 | 否 |
| `testing` | `bool` | `INKTIME_TESTING` | 依 environment | 否 | 否 |
| `worker_concurrency` | `int` | `INKTIME_WORKER_CONCURRENCY` | `2` | 否 | 否 |
| `scheduler_identity` | `str` | `INKTIME_SCHEDULER_IDENTITY` | `inktime-scheduler` | 否 | 否 |
| `cookie_secure` | `bool` | `INKTIME_COOKIE_SECURE` | production 為 `true` | 否 | 否 |

所有相對路徑都相對同一 `base_dir` 解析，再固定為絕對路徑。Port、boolean、timezone、
proxy trust、worker concurrency 與空白 identity 都 fail closed；production 禁止 testing、
development 與 repo 內的預設資料目錄。

API Key、Device Secret／Legacy Device Token、Session Secret 與 Credential 不屬於 RuntimeConfig。它們分別由
`SecretStore`、Device Repository／DevicePairingService 與啟動期持久化 Session Secret 管理；RuntimeConfig 的
`repr`、`diagnostic_summary()`、JSON 與 Log 不包含這些值。`diagnostic_summary()` 會將
路徑與 bind host 遮蔽，只回傳其已設定／已解析狀態及其他非敏感型別化部署欄位，供
`/health/detail` 與三種 process 共用。

測試應以 `RuntimeConfig.from_sources(environ={}, environment="test", ...)` 明確傳入
Temporary Directory、隔離 SQLite／Release／Backup／Cache／照片目錄。不要依賴 import-time
環境變數修改，也不要使用正式 Volume。
