# 模型與長期穩定性完整稽核（2026-10-11）

起點：`e151407`（PR #150 修正自動加入的 user 外鍵後）。本次模型／資料庫／排程修改獨立提交；韌體沒有新增修改。此報告描述原碼檢查與回歸案例，Hosted CI 及部署驗收另列，不能據此保證永遠沒有未知問題。

既定功能保留：分析一張照片，產生台灣繁體中文、克制詩意與留白的文青文字。現有相框使用 `side_caption` 的 8～16 字完整一句；沒有擅自增加長篇描述、第二次 Vision 或文字修復的付費請求。評分、內容分類與方向判斷和短句在同一次 Schema v5 回覆中完成。

## 發現與修復

| 編號 | 問題及影響 | 修復與驗證要求 |
| --- | --- | --- |
| M01 | 原生 OpenAI 的不同模型混用 `max_tokens`／`reasoning_effort`／temperature；不能關閉推理或僅支援 Responses 的模型可能報參數錯誤，或消耗未預期的推理額度。 | 原生 GPT-5/6 與 o 系列用 `max_completion_tokens`；GPT-4o/4.1 不送 reasoning 參數。能使用 none 的推理模型明確送 none；已知不能 none 的模型明確拒絕該設定；非 none 移除 temperature。已知 Responses-only Pro/Codex、純文字 o1-mini/o3-mini 在圖片傳輸前拒絕。相容服務與 OpenRouter 保留自己的策略。回歸測參數、模型快照及送出前拒絕。 |
| M02 | 非法 Token 被轉成 0，可能被記成免費完整用量；快取寫入沒有價格欄位卻可能低估費用。 | 僅接受非負 int64 整數，拒絕 bool、字串、小數、非有限數值、溢位、cached 大於 input、reasoning 大於 output。無法可靠計算時保留 unknown 及費用防護；供應商合法回報金額仍可獨立採用。快取寫入無價格時不能估算。 |
| M03 | 同步與 Batch 分別解析 usage，規則可能漂移；浮點相加會產生額外誤差。 | 共用解析器與 Decimal 費用計算，cached input 只折抵一次，reasoning 已含在 completion 不重複計價；最後才轉成 SQLite REAL。Batch 優先明確 Batch 價格，否則沿用既有折扣設定。這是估算，不是取代供應商帳單。 |
| M04 | 費用預覽以 1200 output tokens 計算，實際最多 512；0.5～2 倍情境被命名為 minimum/maximum，容易被理解為保證界線。 | 同步、Batch、預覽共用上限函式（現行 256～512，受既有設定控制）；保留舊回應欄位相容，新增 `estimate_is_bound=false`、`input_tokens_measured=false` 並明說是情境。回歸檢查 1200/8000/300 設定與實際上限一致。 |
| M05 | 費用保存用實際 served model，但釋放預算保留額度用 requested model；可能提前釋放 unknown 費用或留下已知費用的防護。 | 計價與釋放採同一個實際 served model；沒有該模型確切價格時保留 unknown，不能猜 alias 價格。回歸包含兩種 requested/served 價格差異。 |
| M06 | JSON 重複鍵會被一般解析器以最後值覆寫，衝突的內容分類可能被掩蓋；孤立 Unicode surrogate 會在編碼保存時才失敗；極大整數可能溢位。 | 模型入口、Batch、本機 JSON 抽取與儲存驗證共用拒絕重複鍵／NaN／Infinity 的 decoder；在轉繁體及保存前拒絕不可編成 UTF-8 的字串；先做數值界線再有限值檢查。回歸確認不保存衝突結果、不自動再發付費請求；v4/v5 正常歷史資料仍可讀。 |
| M07 | WAL + synchronous=NORMAL 可在斷電後失去最近已提交交易，已計費但分析／usage 遺失會造成後續判斷錯誤。 | 每條連線採 FULL，保留 WAL、外鍵、busy timeout 與既有公平 writer lock。回歸檢查新連線設定、交易提交及重新讀取。增加 fsync 是明確的耐久性／I/O 取捨；不代表已驗證 NAS 斷電。 |
| M08 | 提示詞有重複敘述；直接替換會使舊凍結計畫的指紋與實際文字不符。 | 共用主提示詞由 886 縮為 672 字元，明確強化文青風格、避免虛構，保留評分／安全／方向規則。新提示詞獨立版本；舊 v5 與 caption 版本的凍結工作仍使用原文字，同步／Batch 都傳遞版本。字元減少約 24.2%，不是已量測的 Token 節省率。 |
| M09 | 定期分析未保存 plan，Worker 會使用空 plan 的 automatic_ai 預設與當時 Provider；建立工作時沒有完整檢查執行模式，違反「設定 API key 不會自行開始付費」。 | Scheduler 建立工作前檢查執行模式、Vision route，凍結模型／Provider／提示詞／規則／執行政策。disabled 拒絕新分析；local_only 與 local_with_manual_ai 拒絕定期雲端分析，即使手動觸發排程也不能繞過。回歸測三種模式與本機計畫。 |
| M10 | 工作選取對完整 plan 做指紋，保存同步結果卻移除 display／repair 欄位；同一計畫完成後仍可能被判為待分析。 | 定義共用的同步結果指紋，沿用原保存格式，API 選取／預覽、Scheduler 與結果保存一致。顯示控制與停用修復不影響完成判斷，提示詞變更仍會過期。回歸測已保存結果不再入選，真正提示詞變更可再入選。候選選取同時接受同一 plan 的同步與 Batch 指紋；Batch 的提交完整指紋保留，避免破壞已上傳的批次請求。 |

## 模型選擇與最省 Token 的界線

- 一張正規化 JPEG、一次 Vision、一次精簡 JSON：沒有自動產生長描述、兩階段圖片分析或付費 JSON 修復。快取、來源內容 hash、single-flight、請求 checkpoint 與模糊付款狀態的防重送規則保留。
- 使用既有配置中支援圖片、Chat Completions、可關閉推理且能可靠輸出 v5 JSON 的模型。GPT-4o/4.1 與支持 none 的新系列採不同參數策略；不以「越新越好」自動升級、不更改實際部署帳號的模型與價格。官方原生 GPT-5 初代／部分新系列不能 none，與目前預設 none 衝突時應換模型或明確選擇支持的推理級別。
- 官方 Pro/Codex 的已知 Responses-only 變體不適合目前 Chat Completions 路徑。沒有為短句引入 Responses、多回合、工具或 reasoning text，避免增加架構與額外請求。
- 全域模型與 Provider 專屬模型有優先順序；實際 Provider 模型優先。不能只看全域名稱來判斷帳單或可用功能；自訂相容服務的能力仍須由服務契約驗證。
- Web 預設最大邊 1024、可選 1600，保留正規化與 EXIF transpose。降低 JPEG 檔案大小只減少傳輸位元組，不能直接推導圖片 Token。圖片計费取決於模型、尺寸、detail 及 patch/tile 規則；512 在底層可用，但未經代表照片驗證不能宣稱不影響辨識與短句品質。
- 不存在已證明的「全球最少 Token 且文案品質相同」模型選擇。此次實際減少可避免的提示詞與重複工作，並限制 output／預設 reasoning；沒有擅自發付費請求。需要代表照片的實際 usage、費用及人工文案品質對照，才能比較不同模型與解析度。
- 費用、Token 數與主觀分數分開：照片回看價值與文案品質不是可證明精準的物理測量。67% 回憶／33% 視覺為既定排序政策，本機技術品質仍只作資格門檻；保留範圍與有限值檢查，未把模型 confidence 當成校準機率。

模型規則依據（2026-10-11 核對）：[GPT-5](https://developers.openai.com/api/docs/models/gpt-5)、[GPT-5.1](https://developers.openai.com/api/docs/models/gpt-5.1)、[GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)、[新模型參數](https://developers.openai.com/api/docs/guides/latest-model#gpt-6-astra-update-api-and-model-parameters)、[GPT-5.2 Pro](https://developers.openai.com/api/docs/models/gpt-5.2-pro)、[GPT-5.2 Codex](https://developers.openai.com/api/docs/models/gpt-5.2-codex)、[圖片 Token 計算](https://developers.openai.com/api/docs/guides/images-vision#calculating-costs)。API 能力可能更新，自訂 Provider 不自動沿用原生 OpenAI 的全部限制。

## 輸出保存、資料庫與規範

1. 檔案資格與來源 hash → EXIF／JPEG 衍生圖 → 送出前再次檢查 never_upload／照片／工作狀態 → 固定 Provider 的單次請求 → 用量／費用 checkpoint → 本機 JSON 抽取 → 嚴格 Schema／Unicode → 分類與人工覆寫規則 → SQLite 交易保存 canonical JSON／欄位／照片狀態 → 快取與排序更新。Batch 收回結果也先驗證再寫入。
2. usage 不完整時不是 0 費用；實際 served model 未定價時保留 unknown。供應商回報有效金額優先。重試保留已取得回覆的 checkpoint，禁止因格式錯誤或不明付款自動重送照片。保存 Schema v5 與舊 v4 可讀相容；沒有改已發布 migration、清空資料或重新計算歷史結果。
3. 外鍵在每條連線開啟、SQL 值綁定、寫入交易／rollback、WAL／FULL、檔案系統 preflight、本機 writer 協調與一致性備份流程保留。SecretStore 為 Fernet 加密，密碼為 scrypt／備援 PBKDF2；改密碼使既有 session version 失效。模型短句與 JSON 在網站使用跳脫輸出。
4. 主密鑰／API key、原始照片及備份屬敏感部署資料。檔案可靠性、密鑰保管、備份隔離、還原演練與供應商資料處理政策不是原碼掃描能代替的驗收。本次沒有讀取使用者資料庫、私密設定或送出私人照片。
5. 實際核對技術契約為 JSON／Schema、SQLite durability 與 Provider API。沒有指定法域、個資處理目的／保存期限或認證標準，不能宣稱符合所有法規或 ISO 認證；也沒有新增要求使用者配對碼或強制複雜密碼。

JSON 依據：[RFC 8259](https://www.rfc-editor.org/rfc/rfc8259.html) 第 4 節要求名稱應唯一、第 6 節排除 NaN/Infinity、第 8 節說明 Unicode 與不可預期字串互通性。拒絕重複名稱是本專案刻意採取的較嚴格入口政策。SQLite 依據：[synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous)：WAL 的 FULL 提供 commit durability；仍依賴底層正確 fsync。

## 全專案來源檢查範圍

| 子系統 | 核對的關鍵邊界／本次結論 |
| --- | --- |
| 模型／同步／Batch | route/model 來源、請求參數、prompt 版本、Schema、Token／金額、快取、預算、防重送、已提交批次不變性；修復 M01～M06、M08～M10。 |
| 照片／掃描／衍生圖 | 安全路徑、來源 hash、解碼限制、EXIF、衍生圖原子寫入及使用中 lease；不更動原圖。 |
| DB／repositories | FK、交易與 rollback、writer lock、migration 相容、結果保存及 unknown usage；修復 M07。 |
| Worker／Scheduler | lease、逾時終止、progress、重試分類、定期去重、備份與清理節流；修復 M09/M10。 |
| 發布／渲染／裝置更新 | 鎖內 snapshot、鎖外 hash 後重驗、原子 pointer／fsync、保留有效 release、恢復與 GC；沿用 PR #150 修復。 |
| 備份／還原 | SQLite backup snapshot、manifest／hash／筆數驗證、runtime exclusive lock、安全副本、相容升級與拒絕降版；未執行真正還原。 |
| 網站／登入／權限 | CSRF、Strict/HttpOnly cookie、部署 Secure 設定、session version、登入限流、redirect、本機路径與文字跳脫；保留純數字密碼与自動加入。 |
| 通知／網路 | webhook DNS pinning、私網政策、timeout、敏感資訊遮蔽；沒有發送通知或改部署網路。 |
| 執行／部署／相依套件 | runtime preflight、避免網路 DB mount、服務 healthcheck／build revision、既有安全修補與 Hosted gates；沒有更新 NAS 或容器。 |
| 韌體 | 本次沒有新增韌體修改；F01/F02 已在 PR #150，F03 實測與舊 13C F04～F09 條件限制見 [韌體稽核](FIRMWARE_REVIEW_20261011.md)。沒有刷機／實機驗收。 |

以上是子系統與關鍵呼叫路徑的來源稽核，不是逐行形式化證明。未發現能從目前證據確定的其他新增缺陷，不把未執行的部署驗證視為成功。

## 驗證與交付界線

- 本機只做修改檔 Python AST、diff whitespace、文件導覽／契約 validator。未執行本機 pytest、build、安全掃描、長時間運轉或韌體編譯，依 AGENTS 交由 Hosted CI。
- 新增回歸涵蓋非法 usage／數值、同步與 Batch 一致、native 模型參數、凍結提示詞、重複 JSON／Unicode／溢位、費用防護、同步指紋、排程執行模式與 FULL 連線設定。
- 另已修復 PR #150 既有 Hosted failure：LAN 自動加入不再把虛構的 `system:lan` 寫進 users 外鍵；改用 NULL 加既有活動來源，保留 FK 約束。舊 UI 文字斷言同步現行流程。
- 本次新增修改以 Draft PR 交付，不自動合併。Hosted CI 僅提交後檢查一次，進行中為 `CI_PENDING`；是否通過依該提交的 GitHub checks。PR #150 的使用者已授權合併與本次新增範圍分開。
- 未驗證：實際部署的模型配置／服務可用性、真實 Token／文案品質、持續運轉、NAS fsync／斷電、備份還原演練、相框實機。不能承諾「完全沒有問題」或已取得任何合規認證。
