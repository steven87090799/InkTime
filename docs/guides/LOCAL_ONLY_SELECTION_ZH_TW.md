# 本機無 AI 選片與雙照片文字版型

操作入口：administrator →「設定」`/settings` → 搜尋「分析執行模式」或 `analysis.execution_mode`；找不到時切換進階並清除篩選。切換後建立新工作，既有已完成的本機工作不會自動變成 AI 工作。

InkTime 的 `analysis.execution_mode` 是分析行為的權威設定。新安裝預設為
`local_only`：Scanner 仍會擷取 EXIF、方向、本機品質與 E6 適合度，但不建立
Provider Router、不讀取 Provider Secret、不保留 AI Cache、不建立模型請求，也不寫入模型費用。

可選模式為：`disabled`、`local_only`、`local_with_manual_ai`、`automatic_ai`。
舊的 `analysis.ai_mode` 仍可使用；更新它會安全對應到新的模式。只有
`automatic_ai` 會依既有排程自動使用已設定的 Provider。

`disabled` 比 `local_only` 更嚴格：它仍可顯示既有照片、既有分析與既有 Release，但 API、Worker 與
Analysis Service 都會拒絕建立或執行新的分析，不會執行 prefilter 或 local fallback，也不會新增
`photo_analysis`。`local_only` 則允許 Scanner 的本機特徵與本機選片；管理員明確指定 active、eligible、
Library 已啟用、未排除、local features 完成且原始檔存在的照片時，可直接建立 Release，不要求
`photo_analysis`。

正式發布的資格會逐張判定，而不是整批 fallback：`automatic_ai` 僅接受既有有效 Analysis；
`local_only`、`local_with_manual_ai` 與 `disabled` 可在同一個雙照片 Release 中混用
`analysis` 與已完成 Scanner Local Features 的 `local`。每張選定照片的來源會凍結於 Render Plan。

本機選片會先由 SQLite 有界取得至多 `render.local_candidate_limit`（預設 200）張
完成本機特徵、active、eligible 且原始檔存在的照片。`local_display_score` 保留原始
`local_candidate_score`，再加上最愛、歷年今日、未近期顯示、方向與回饋調整，並扣除
近期顯示、低優先與電子紙風險。選片與雙照片配對是 deterministic；其前 50 個候選與
所有 component 會寫入既有 Decision Trace。無同日候選時會記錄 ranked fallback。
歷年今日依序採 Exact Day、設定視窗內的 Nearby Day、再依設定採 Ranked Fallback；`nearby_only`、
`ranked` 與 `none` 不會偷偷放寬。非閏年要求概念上的 2 月 29 日時，會回退為 2 月 28 日並記錄原因。
Pair 只在前 50 個候選內計算，保存方向相容、日期接近、已知地點相同、雙低優先／高風險／近期顯示等
Pair Score components；停用 Library 不會進入候選、Pair 或 Trace。
Pair Secondary 只會從實際 Allowed Pool 的前 50 張中比較；Exact 已足夠時不會被 Ranked 取代。
Trace 會分開保存 Requested／Effective Month-Day、Leap Day 回退、實際 Primary／Secondary stage、
各 stage／Allowed Pool 數量與 Pair candidate 數。地點加分只使用離線 LocationResolver 的粗略城市名稱；
沒有可靠資料時為零，不會寫入精確 GPS、猜測路徑或呼叫網路。

`photo_pair` 是「雙照片・純照片」：不保留文字區。`photo_pair_caption` 是「雙照片・各自一句話」：
直向時各卡片的照片下方各有一段文字；橫向時左右欄各有一段文字。每段文字都是獨立 Caption
Record，保存自己的 `photo_id`、來源、版本與雜湊。優先使用該照片已有的短文案，否則只根據
自己的日期與已知地點產生本機文字；不推測人物、關係、情緒、事件或地點。

Preview 與正式 Release 都從同一份 Render Plan 讀取 Primary/Secondary、兩段 Caption、Crop、
Subject Box、方向、Profile 與 Effective Dither。因此背景 Preview、Device Release 與 Cache Hit
不會重選第二張照片或重新解析不同 Caption。可在「相框與 Release」頁選擇四種組合：直／橫向
的純雙照片與各自一句話；系統使用正式 Renderer，而非 Browser Canvas。

管理頁的「雙照片版型比較」會以單一 Frozen Compare Plan 產生四張正式 Background Preview：直／橫向
`photo_pair` 與直／橫向 `photo_pair_caption`。四張固定使用相同的 Primary、Secondary、Caption A、
Caption B、Profile 與 Effective Dither；純照片卡片明確標示不顯示文字。

Local Caption 先把 aware datetime 轉至 `general.timezone` 再判斷日期；純 date 不會位移，無效 IANA
時區安全回退 UTC。人工 Caption、已知 AI Caption 與來源不明的既有 Caption 分別保存來源；來源不明
不會冒充 AI。正式 Preview／Release 會驗證 Caption A 與 Caption B（連同日期、地點與固定文字）的
字型覆蓋，缺字會明確失敗而不靜默換字型。

正式 Caption 的 AI provenance 會保存安全的 provider、model、stage、prompt version、schema version
與更新時間；Local 與來源未知的舊文字不會被標為 AI。Release Metadata 不保存 API Key、Token、Raw
Provider Response 或原始 Prompt。

GitHub Workflow 會執行完整測試，但目前未啟用 `--cov-fail-under=80` Coverage Gate；本機嚴格 Coverage
須另外執行，並在結果可信時才可報告數值。Docker CI 僅驗證 Web health，Worker／Scheduler 需由獨立
三服務驗證另行證明。

要日後啟用自動 AI，先設定可用 Provider，再將模式明確改成 `automatic_ai`。可從 Decision Trace、
Job 記錄與 Usage 查核 local_only 的 Provider Router、模型呼叫、Secret 讀取、AI Cache Reservation
與 Usage Cost 都為零。
