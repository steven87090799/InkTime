# 本機模式與正式照片資格

新安裝預設 `analysis.execution_mode=seasonal_ai`，按顯示日期準備歷年同期照片；可用 Provider、待處理內容與預算均滿足時才會送圖。僅設定 Key 不代表立即發生請求。若只做本機特徵，請明確改為 `local_only`。

可選 `disabled`、`local_only`、`local_with_manual_ai`、`automatic_ai`、`seasonal_ai`。`disabled` 拒絕新分析；`local_only` 不呼叫 Provider；`local_with_manual_ai` 允許明確的單張手動 AI。OpenAI 缺少資訊補齊與 OpenRouter 同期準備各有獨立設定、預算與暫停狀態；未知付費結果須先對帳，不自動重送。

正式相框照片須經目前 v6 內容安全與視覺方向守門；v4/v5 歷史結果保留可讀，不代表具備目前正式發布資格。低信心安全或方向結果須人工確認。高分、最愛與本機覆寫不能跳過正式安全守門。`photo_only` 測試／本機流程是另有明確限制的用途，不等於正式自動顯示資格。

現行固定排名使用 AI 回憶價值 0.67、視覺 0.33 與特殊加分；本機品質只作門檻，E6 不混入排序。舊 LocalSelectionPolicy 的本機加權與混用正式發布流程已退場，不能依其歷史描述設定現行系統。

一般照片完整分析只送一次圖片，不另呼叫第二個模型審稿。Caption 是至多20字的一句完整文句，不設固定8–16字或4–6字下限。橫向相框維持兩張直式照片、小字文句、地點直排於底部日期上方。

預覽與正式輸出使用凍結 Render Plan 的照片、Caption、方向與色彩設定；預覽色彩模式亦屬快取身分。工作、付費證據、Release 與 Trace 各自保存，不把 CI 成功當成實體面板驗收。

操作入口為「設定」搜尋 `analysis.execution_mode`；詳細設定與預設以[現行基線](../reference/CURRENT_STATE_ZH_TW.md)及設定 metadata 為準。舊 CI／本機流程說明不再作為當前驗收規範；遵循目前 AGENTS.md 與 GitHub 工作流程。
