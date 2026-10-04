# 2026-10-04 問題清單逐項核對與修補

BASE_HEAD=bca27aa796e3b0ba213e416b81936bb6f9cf7a67
BRANCH=fix/audit-remediation-20261004

本報告對應使用者提供的 R01–R12、U01–U10。原清單是追蹤入口；成立判斷來自該基準程式與其直接契約。下表「修補」指程式已修改，測試案例已加入，尚不代表 Hosted CI 或現場驗收完成。

## 確認成立並修補

| 項目 | 核對結論與修補 | 驗證入口 |
|---|---|---|
| R01 | Timeout 與非 ProviderHTTPError 的確有帳本清理缺口。建構錯誤標記明確 not_sent；隔離層保留此證據，不再把缺少 metadata 當成未送出。分析與診斷只依明確未送出證據釋放，POST 後不明結果仍保留。 | test_billable_operations、test_analysis_pipeline 的 builder/capacity/consumed regressions |
| R02 | 不合格回應確實被 checkpoint 重用。一般重試保留回應；新增管理員付費操作頁與有原因、額外費用確認的重送批准，建立新 operation ID 並保留舊回應及帳務。force_recompute 不再提前繼承其他照片分析。 | checkpoint approval、invalid checkpoint、force inheritance regressions |
| R03 | 暫時畫面與恢復照片確實缺少 disp_pending。所有 PhotoPainter 電量／配對入口先持久化同一 marker；恢復成功才清除，失敗或中斷保留。marker 已設置時不重複寫 NVS。 | firmware power contracts；仍需實機斷電驗收 |
| R04 | 價格鍵與模型名稱混用成立。先選模型再載同鍵價格；切換與 URL 指定均重載；未知價格顯示空欄且要求完整輸入，零價格仍有效。價格保存加入原始快照比對。 | pricing snapshot regression；Hosted UI/static checks |
| R05 | 固定 0.01 單價成立。預估使用計畫的首選 Provider／實際模型價格與目前輸出 cap；本機為零、未知價格為 null。明示 Token 假設與未扣除快取，保留單一圖片／零修復計數；舊預估回應不覆蓋新條件。各 fallback 路線另列預估，任一路線無價格時不提供已知最大費用；Token 假設不是實際帳單保證。 | model-price/local/unknown estimate regression |
| R06 | 提示／Schema 洩露答案成立。圖片隨機選擇 1–3 個圖形，答案只在 runner；格式、視覺正確性與 usage 分開。Level 3 只證明完整格式，不授予未驗證的視覺正確標章。 | pixel-reading stub、blind guessing、blank/changed shape controls |
| R07 | local_next 短按與 UI 提示不一致成立。API 改為 hold_key1_for_sync，UI 說明強制連網與等待同步，保留離線短按；區分 1.2 秒與 4 秒配網維修門檻及開機延遲。 | device API/UI contracts；仍需實機手勢驗收 |
| R08 | 工作缺少前端去重、診斷缺少持久結果恢復、字型 exception 不恢復成立。工作與發布使用同鍵重試；診斷用持久 operation checkpoint，未知請求不自動重送，同鍵變更內容拒絕。busy/finally 覆蓋診斷、Provider、價格、裝置、字型、備份與發布操作。 | durable key conflict/approval tests、Hosted UI contracts |
| R09 | 空白值無法表達清空成立。提供保留／更換／清空選項，空白更換拒絕；舊請求未帶新欄位仍維持原行為，舊密碼不回填。 | existing config-store tests、portal source contract |
| R10 | 分鐘與非整小時偏移往返缺口成立。提供全部分鐘與以分鐘儲存的時區選項，保留現有特殊偏移；省略欄位保留，非法數字拒絕；繼續使用既有原子 Config Store，不改資料格式。 | C++ portal integer cases 03/17/+05:45/-05:30/invalid；Hosted firmware compile |
| R11 | 未登入共用 context 查 critical 告警成立。只有管理員查詢並呈現詳細告警；公開登入仍顯示服務可用性摘要。 | public login marker vs administrator HTML regression |
| R12 | 面板 helper 轉大寫與混合大小寫 AP 不一致成立。配網 AP 改為 INKTIME-<suffix>，與面板一致；家中 SSID 不變。 | portal source contract；仍需實際廣播名稱比對 |

## 其他風險與優化逐項結論

| 項目 | 核對與處理 | 證據邊界 |
|---|---|---|
| U01 | 快照無法包括快照後外部費用。交換 DB 前 fsync 持久的付費暫停 marker（含 exact snapshot）；分析、診斷與 Batch 均有 gate。管理員對帳頁明確批准後留下獨立批准檔。舊 started 操作仍逐筆阻擋，不自動釋放預留。 | 還原 fixture 與 gate regression；真實供應商對帳必須由管理員完成 |
| U02 | latest 原本無 Release 就 404，無法取得其中設定。改回 authenticated no_content manifest + device_config；韌體持久化設定後保留原畫面，正常 status 回報 applied_config_version。 | empty-library manifest regression；相框空庫同步與 ACK 尚待實機 |
| U03 | 入隊選片有過濾，但單張分析最後送出點缺少 current never_upload／取消檢查。加雙層檢查，隔離 OpenAI 在 body 建好後、POST 前重查 DB；Batch 上傳前與 create 前重查。已開始傳輸的上傳與費用不宣稱可撤回。裝置重新配對另有 credential version/auth guard，保留。 | mutable consent/cancel regression；HTTP 起始後變更不撤回已送資料 |
| U04 | 配對已有交易與期限保護；設定／Provider／價格舊表單缺少版本比對，已補。發布 pointer snapshot、DB commit、補償原先未由同一 metadata lock 保護，可能回復覆蓋另一個發布，已串成跨程序交易範圍；回滾同樣持鎖。 | settings/provider/pricing conflicts；existing release compensation/concurrency suites |
| U05 | 現有 offline_schedule_core 驗證重試 horizon 與 Slot，深睡有 24 小時上限；TLS transport 使用 CA，沒有 setInsecure。固定 offset 是既有契約；未證實需更換 IANA 或關閉驗證。 | existing offline retry、schedule、TLS contracts；RTC 回撥/CA 過期實機場景未量測 |
| U06 | FFat 只在整個分區為 erased 時格式化，帶資料 mount failure 回報 STORAGE_CORRUPT；GC 保護 active/staged/last-good/in-flight/recovery frame。既有缺內容與儲存錯誤不同。新增 U02 no_content 明確保留畫面。 | storage bus/core contracts；真實磁碟滿、損毀與長期離線待現場驗收 |
| U07 | 帳本有 state/content/request 索引，retention 對 unknown 與 operation_id/batch_item_id usage 保留；Trace 活躍關聯亦有保護。未證實失控，不刪除對帳證據。新的待處理操作頁有分頁，不把舊操作永久藏在前 100 筆外。 | resilience cleanup、ACK/storage budgets、paid-state backup tests；長期成長率未量測 |
| U08 | 這是品質/成本實驗而非已證實 bug。保留 v4/v5 可讀、固定排名、正常單圖片且不自動 repair、不直接描述畫面的文案風格，未以無依據的 prompt 變更使快取失效。R06 修的是診斷有效性，沒有增加正式照片回合。 | existing schema/scoring/plan tests；代表照片與實際 Provider 品質尚未量測 |
| U09 | connectionHintChanged helper 原本未接 saveWiFiFastPathHint，醒來重寫相同 hints；已接成只在 channel/BSSID 變更時寫。R03 marker 亦不重寫已設置值。連網原有總 12 秒與 fast-path 上限；不以未使用的其他 helper 宣稱耗電策略全部已生效。 | existing power/core tests與新增接線；電流、heap peak、實際 NVS 次數需硬體量測 |
| U10 | process_boundary 有有界 semaphore、timeout/cancel terminate、join 與 finally slot 回收，100k harness 有 CPU/RSS/claim/cancel 量測。補 FD 基線/結束、子程序結束數與 WAL 結束值至 Hosted benchmark 報告；未因未量測瓶頸移除 process isolation。 | Hosted nightly benchmark；這些結束值不是峰值，不能代表真實 NAS 長期 soak |

## 驗證與交付

本機僅做 Python/JavaScript 語法、diff 與相關文件 validator；依 AGENTS.md，不跑本機 pytest、Docker、Playwright、韌體 compile、付費 Provider 或長期 soak。上述測試檔由 Hosted CI 執行。PR 保持 Draft；不合併、不部署、不刷機。

暫時畫面斷電、KEY1 實際手勢、AP 廣播、空庫設定 ACK、耗電/heap、RTC/TLS 故障、NAS 真實長期資源與代表照片品質，仍是現場驗收或量測事項。沒有這些證據時不標成已實機修復或成本節省已達成。
