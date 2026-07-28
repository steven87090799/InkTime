# Shadow Mode

Shadow 預設關閉。以 `PUT /api/shadow/config` 啟用後，Scheduler 以穩定雜湊決定抽樣（10/25/50/100%），每日受最大次數限制；結果只建立 `shadow` Decision Trace，不建立 Release、不變更 Assignment、Display History、正式 Queue 或通知。

停用即停止新任務，既有 Trace 依保留政策清除。Shadow 維護失敗只留下 `SHADOW-001` 結構化事件，正式換圖不受影響。
