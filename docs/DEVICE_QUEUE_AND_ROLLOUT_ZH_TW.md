# 裝置 Queue、Canary 與 LKG

離線 Queue 使用單調 queue version 和每項 idempotency key。只有 `DISPLAY_COMPLETED` ACK 能完成項目、寫入顯示歷史並更新 Last Known Good；重複、錯誤裝置、未知項目或錯誤 key 的 ACK 均拒絕或冪等處理。

Canary 失敗門檻達成時停止擴散並進入回滾流程；Release 不刪除，以保留稽核資料。實際 ESP32 與電子紙全刷、BUSY 時序、Wi-Fi 斷線復原仍須在實體面板完成驗證。
