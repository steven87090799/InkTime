# 本機無 AI 選片與雙照片文字版型

InkTime 的 `analysis.execution_mode` 是分析行為的權威設定。新安裝預設為
`local_only`：Scanner 仍會擷取 EXIF、方向、本機品質與 E6 適合度，但不建立
Provider Router、不讀取 Provider Secret、不保留 AI Cache、不建立模型請求，也不寫入模型費用。

可選模式為：`disabled`、`local_only`、`local_with_manual_ai`、`automatic_ai`。
舊的 `analysis.ai_mode` 仍可使用；更新它會安全對應到新的模式。只有
`automatic_ai` 會依既有排程自動使用已設定的 Provider。

本機選片會先由 SQLite 有界取得至多 `render.local_candidate_limit`（預設 200）張
完成本機特徵、active、eligible 且原始檔存在的照片。`local_display_score` 保留原始
`local_candidate_score`，再加上最愛、歷年今日、未近期顯示、方向與回饋調整，並扣除
近期顯示、低優先與電子紙風險。選片與雙照片配對是 deterministic；其前 50 個候選與
所有 component 會寫入既有 Decision Trace。無同日候選時會記錄 ranked fallback。

`photo_pair` 是「雙照片・純照片」：不保留文字區。`photo_pair_caption` 是「雙照片・各自一句話」：
直向時各卡片的照片下方各有一段文字；橫向時左右欄各有一段文字。每段文字都是獨立 Caption
Record，保存自己的 `photo_id`、來源、版本與雜湊。優先使用該照片已有的短文案，否則只根據
自己的日期與已知地點產生本機文字；不推測人物、關係、情緒、事件或地點。

Preview 與正式 Release 都從同一份 Render Plan 讀取 Primary/Secondary、兩段 Caption、Crop、
Subject Box、方向、Profile 與 Effective Dither。因此背景 Preview、Device Release 與 Cache Hit
不會重選第二張照片或重新解析不同 Caption。可在「相框與 Release」頁選擇四種組合：直／橫向
的純雙照片與各自一句話；系統使用正式 Renderer，而非 Browser Canvas。

要日後啟用自動 AI，先設定可用 Provider，再將模式明確改成 `automatic_ai`。可從 Decision Trace、
Job 記錄與 Usage 查核 local_only 的 Provider Router、模型呼叫、Secret 讀取、AI Cache Reservation
與 Usage Cost 都為零。
