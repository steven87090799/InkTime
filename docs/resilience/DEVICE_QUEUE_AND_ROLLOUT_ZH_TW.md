# 裝置 Queue、Canary 與 LKG

Queue 使用單調 queue version 和每項 idempotency key。只有 `DISPLAY_COMPLETED` ACK 能完成項目、寫入顯示歷史並更新 Last Known Good；current/LKG 依 `displayed_at` 時間單調前進，順序相反的晚到 ACK 只能補歷史，不能倒退指標；同一時間若出現不同 Release 則拒絕。重複、錯誤裝置、未知項目或錯誤 key 的 ACK 均拒絕或冪等處理。

裝置切換 `legacy_online`／`stock_compat` 與 `inktime_offline_schedule` 時，中央 Repository 在同一 transaction 取消不相容的 active delivery，並寫入 `DELIVERY_MODE_TRANSITION_CANCELLED` audit event；Enhanced 裝置不會被 generic Canary rollout 或 online rollback queue targeting。它們必須經由 offline `prepare_day()` 與 schedule snapshot 交付。

Canary 失敗門檻達成時停止擴散並進入回滾流程；Release 不刪除，以保留稽核資料。實際 ESP32 與電子紙全刷、BUSY 時序、Wi-Fi 斷線復原仍須在實體面板完成驗證。
