# 備份與還原

## 備份內容

Web「備份與還原」建立的 ZIP 使用 SQLite online backup API 取得一致快照，並包含：

- `inktime.sqlite3`：照片狀態、分析結果、最愛／標籤相關資料、工作與排程狀態、顯示／發布歷史的資料庫紀錄及一般設定。
- `settings.json`：可讀的一般設定匯出，不含 Secret。
- `manifest.json`：備份格式、應用程式版本、Database Schema Version、每個檔案 SHA-256／大小及重要資料表筆數。

預設不包含 API Key、Webhook Token、原始照片、縮圖快取、已渲染的 Release 檔案或 Log。Secret 會從備份用的 SQLite 副本刪除並執行 secure delete／VACUUM；還原後請在 Provider／通知設定重新輸入。原始照片與 `/data/releases` 仍須使用 NAS snapshot 或其他檔案備份制度；SQLite 內的顯示／發布歷史紀錄會保留，但沒有對應 Release 檔案時不能直接回滾到該輸出。

## 建立與驗證

1. 以 administrator 登入 Web，進入「備份與還原」。
2. 按「立即備份」，完成後下載 `inktime-backup-*.zip`。
3. 應用在建立時已驗證 ZIP 成員、SHA-256 與 SQLite `PRAGMA integrity_check`；仍建議把下載檔複製到另一個實體儲存位置。

不要手動修改 ZIP 內任何檔案；manifest 雜湊不符時還原工具會在碰觸現有資料庫前拒絕。

## Docker 離線還原

以下命令從 InkTime 專案根目錄執行。檔名請換成實際位於 `${INKTIME_DATA_PATH}/backups` 的備份：

```bash
docker compose stop inktime-web inktime-worker inktime-scheduler
docker compose run --rm --no-deps inktime-web \
  python scripts/restore_backup.py \
  /data/backups/inktime-backup-YYYYMMDDTHHMMSSffffffZ.zip \
  --database /data/inktime.db \
  --backup-dir /data/backups \
  --yes
docker compose up -d
```

還原工具會依序：

1. 取得 exclusive runtime lock；任一 Web／Worker／Scheduler 尚在執行就以 `RESTORE-001` 停止。
2. 驗證備份格式、固定檔案清單、SHA-256、Schema Version、SQLite integrity、Migration 狀態及重要資料表筆數。
3. 用 SQLite backup API 建立目前資料庫的 `inktime-pre-restore-*.sqlite3` 安全副本。
4. 必要時只在暫存資料庫執行舊版 Schema Migration，再 checkpoint WAL。
5. 原子替換正式資料庫，重新執行完整性與筆數檢查。
6. 任一還原後檢查失敗時，自動以安全副本回復原資料庫；安全副本會保留供人工查核。

啟動後依序確認：

```bash
docker compose ps
curl -fsS http://127.0.0.1:${INKTIME_PORT:-8765}/health/ready
```

再登入 Web 確認照片總數、最近分析結果、工作／排程狀態與發布歷史，並重新輸入 Provider API Key、Webhook Token。確認完成前不要刪除 `inktime-pre-restore-*.sqlite3`。

## Migration 回滾

有待執行 Migration 時，啟動流程會先建立 `/data/backups/inktime-pre-migration-*.sqlite3`。若啟動顯示 `MIGRATION-002`，不得強迫 Worker 繼續寫入；停止三個服務後，以同一工具還原原始 SQLite 快照：

```bash
docker compose run --rm --no-deps inktime-web \
  python scripts/restore_backup.py \
  /data/backups/inktime-pre-migration-YYYYMMDDTHHMMSSffffffZ.sqlite3 \
  --database /data/inktime.db \
  --backup-dir /data/backups \
  --yes
```

接著切回與該 Schema 相容的舊映像／Git Commit，再啟動服務。不可只切回程式碼而保留較新的正式資料庫，也不可在線上複製單一 `.db` 檔取代 WAL 一致備份。
