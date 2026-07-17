# 備份與還原

備份 ZIP 含一致的 SQLite backup、一般設定、加密 Secret、裝置、工作與發布中繼資料；不含原始照片、縮圖快取與 Log。建立後會執行 `PRAGMA quick_check`。

還原步驟：停止 web／worker／scheduler；解壓到暫存目錄；確認只有 `inktime.sqlite3` 與 `manifest.json`；對資料庫執行 `quick_check`；備份目前 `/data`；替換資料庫；修正 UID 10001 權限；啟動 web 並確認 Migration／ready；最後啟動 worker 與 scheduler。切勿在線上替換使用中的 SQLite。

排程預設每日 03:00、保留 14 份，可由設定頁調整。原始照片必須另用 NAS 快照／異地備份保護。
