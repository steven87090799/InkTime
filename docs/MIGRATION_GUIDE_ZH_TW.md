# 從舊版 InkTime 遷移

1. 停止舊分析與 cron，備份 `photos.db`、`config.py`、輸出與裝置設定。
2. 執行 `python scripts/migrate.py --database <photos.db>`；舊 `photo_scores` 不會刪除。
3. 啟動新版並建立管理員，再執行下列舊設定匯入工具。
4. 由「維護」掃描照片；SHA-256 可在路徑移動後保留結果，相同內容建立繼承來源。
5. 升級 ESP32 韌體、建立每台 Token、驗證 Manifest 後才移除舊 URL 金鑰。
6. 用小型本地／Mock 工作驗證，再恢復大量分析。

回滾：停止三服務、驗證 pre-migration 備份、恢復舊 DB／映像／config，短期切回舊韌體。舊 API 有明確安全風險，只可在隔離網路使用。詳見 `MIGRATION_PLAN.md`。

## 匯入舊 `config.py`

先用 dry-run 確認範圍，再正式寫入：

```bash
python scripts/import_legacy_config.py ./config.py --database data/inktime.db --data-dir data --dry-run
python scripts/import_legacy_config.py ./config.py --database data/inktime.db --data-dir data
```

工具會匯入時區、渲染門檻、顯示數量、字型、舊 API 開關與 `API_CHANNELS`。API Key 直接以目前 `session.key` 加密，不會輸出到 Console；若尚無該檔案，請先啟動一次或設定 `INKTIME_SECRET_KEY`。

`DOWNLOAD_KEY` 不會轉成新版 Token。請在裝置頁逐台建立 Token；舊 API 維持預設關閉。

## Migration 25：帳號正規化與 Session 撤銷

Migration 25 會為既有使用者補上 `normalized_username`、`session_version=1` 與 `disabled_at`，並建立正規化帳號唯一索引。原本的 `username COLLATE NOCASE UNIQUE` 已阻止 ASCII 大小寫碰撞；若升級資料仍違反新索引，Migration 會完整 rollback 並保留升級前備份，不會刪除或覆寫帳號。

升級前建立的 Session 沒有 `session_version`，因此升級後會失效一次並要求重新登入。之後停用／重新啟用帳號、變更或重設密碼、變更角色都會遞增版本並立即撤銷既有 Session。舊帳號與舊密碼仍可登入；新建帳號與變更密碼才套用 3–64 字元帳號與 12–128 字元密碼規則。
