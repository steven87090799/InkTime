# GPS 地址、地標與模型選擇實驗

日期：2026-10-22（Asia/Taipei）。基底：`17563d7`；分支：`codex/gps-location-landmark-20261022`。

## 決策與範圍

1. 地址由反向地理編碼取得；不用語言模型猜 GPS 地址，也不為每張照片開網路搜尋。
2. 使用者已確認：完整地址放照片詳情，相框只顯示「知名地標／行政區・拍攝日期」。山區地址仍保存供詳情查看；地圖缺門牌時不補造門牌。
3. 模型地標名稱與信心獨立儲存，不影響 memory_score、visual_score、special_level 或安全分類。原型顯示門檻暫定 0.90，這是待校準的規則，不是正確率保證。低於門檻退回行政區／城市；附近 POI 不直接當作照片裡的地標。
4. 預設只向模型提供概略地名；精確座標可在離線請求建構器選擇加入，完整地址不送模型。GPS 是相機位置，可能與遠處被拍攝的地標不同。
5. 舊照片先補地址，額外模型 Token 為 0。新照片才測試把地標放進原本同一個分析請求，避免重傳 10 萬張舊照片。
6. 維持 GPT-6 Luna 作為比較基準；未更動任何部署的 Provider、模型、費率或執行模式。

這是可以審查的原型，**不是自動啟用的新分析協定**。正式 v4/v5/v6 Schema、凍結 Batch、付費 checkpoint 與重試規則均維持原樣。實驗請求使用 `{analysis: <原 v6>, landmark: {name, confidence}}` 外層；匯入器只存獨立地標，不把整個外層誤存為正式分析。尚未接入正常 Worker／Batch 上傳：真正啟用前必須完成版本化、每張照片的地區輸入指紋、證據保存與回應匯入整合。

## 已實作

- Migration 71 新增 `photo_locations` 與 `reverse_geocode_cache`；無全庫回填。照片地點綁定照片 SHA-256 與精確 GPS；檔案／座標改變後舊資料失效。純視覺地標也能用在沒有 GPS 的照片。
- 地址與信心資料使用獨立資料結構，完整地址、行政區、城市、來源、地圖精度、地標名稱／信心／來源各自保存。反向地理編碼回傳的建物精度代表地圖結果，不能當作確切攝影地址已驗證。
- 單照片實驗工具可明確查詢 Geoapify，有限回應大小、連線／讀取 timeout、不自動重試；使用環境變數 `GEOCODING_API_KEY`，不輸出帶 Key 的失敗 URL。
- 精確座標、Provider、語言、資料結構版本共同組成持久快取鍵；不把 100m 格網中的同一地址套到相鄰門牌或行政邊界。地圖無覆蓋也會快取；暫時 HTTP 失敗不快取。
- 相框／照片詳情只讀本機資料，渲染絕不呼叫地圖或模型。相框沿用原本日期最右，地點接在日期前，優先縮短過長地點而保留日期；完整地址僅出現在詳情。
- 沿用 `render.show_location` 開關；沒有新增地點資料時維持原本離線最近城市作為粗略 fallback。離線最近城市不是行政邊界驗證，不能用它宣稱已取得新店區或完整地址。

## 排版修正

使用者指出第一版橫向示意錯誤；已撤回側邊直排文字的設計。已核對現行 [幾何](../../inktime/app/domain/rendering/layout_geometry.py) 與 [渲染](../../inktime/app/services/rendering.py)：`auto_photo` 的兩種橫向模式都是每張照片下方 **28 px** 資訊列，短句靠左，地點與日期靠右。雙直圖中間保留原有 8 px 間距。沒有修改正式幾何。

![四種排版修正示意](gps-location-layouts.png)

橫向示意直接使用專案現行 `resolve_layout_geometry`／`with_photo_date_footers` 座標。直向兩種示意保留使用者認可的方向：單直圖下方文字與日期、雙橫圖上下排列且各有下方文字與日期。直向示意的留白／資訊區高度不是實際面板驗收，合成插圖也不是模型辨識結果。

## Token 與 10 萬張增量成本

離線使用相同 1024×768 合成輸入，從現有 OpenRouter 請求建構器建立基準後，只包入地標外層。`tiktoken` 對 system、user 文字及完整輸出 Schema 計數，不包含圖片 base64；圖片完全相同，沒有額外圖片 Token／圖片解碼。這是可重現的文字 Token 代理量測，**不是 API 帳單或品質實測**；Provider 的 schema framing、實際回應長度／Tokenizer 可能不同。

| 變更 | o200k_base 額外輸入 | cl100k_base 額外輸入 |
|---|---:|---:|
| 地標規則與輸出外層，無地區提示 | 183 | 179 |
| 加「臺北市信義區」 | 200 | 199 |
| 再加數值 GPS | 213 | 211 |

可見輸出外層／地標欄位樣本額外 13 Token（無地標）、19～20 Token（臺北101）。更長地標名稱會增加輸出。計費公式（單位 USD／百萬 Token）：

```text
增量費用 = 張數 × (新增輸入 Token × 輸入單價
                 + (新增可見輸出 Token + 新增推理 Token) × 輸出單價) / 1,000,000
```

以下以每張 +200 輸入、+19 可見輸出，10 萬張原本就需要分析的**新照片**為例，不含原本照片分析費、反向地理編碼費、平台儲值／稅費，不假定快取折扣：

| OpenRouter 路徑 | 輸入／輸出每百萬 | 無額外推理 | 每張額外 512 推理 Token |
|---|---:|---:|---:|
| GPT-6 Luna，Standard | $0.10 / $0.50 | **$2.95** | $28.55 |
| Nex-N2.5-Mini | $0.025 / $0.10 | $0.69 | $5.81 |
| DeepSeek V4.1 Flash，Baidu FP8 促銷路徑 | $0.0342 / $0.1368 | $0.94 | $7.95 |
| Gemini 2.5 Flash-Lite，Standard | $0.10 / $0.40 | $2.76 | $23.24 |

512 是敏感度情境，不是量到的推理用量。GPT-6 Luna 若每張多 2,000 推理 Token，此增量變 $102.95。OpenRouter 的 `reasoning.exclude=true` 只隱藏推理，仍收費；本專案已送 `reasoning: {effort: "none"}`，原型也保持相同設定與輸出上限。實際供應端是否如預期執行仍需查 usage。快取寫入另有費率，不能把「未算快取折扣」誤稱硬上限；例如 Luna 新增 200 Token 全以 1.25 倍快取寫入價計算，樣本成本約 $3.45。保守可先抓 **$3～5／10 萬張**，前提是關閉推理且維持相近短輸出。

機器可讀量測：[token-cost.json](gps-location-token-cost.json)。本地付費模型／地址 API 呼叫數均為 0。GPS 只讀已存 EXIF，無額外全尺寸解碼；實驗建構器複用同一圖片。正式低負載／10 萬張吞吐、地圖命中率與實際費用尚未量測。

## 地址服務與更省錢路徑

Geoapify 官方目前一個 reverse geocode = 1 credit，免費方案每日 3,000 credits；10 萬個不同座標約 34 天。API 10 方案每月 US$59、每日 10,000 credits，約 10 天可處理 10 萬個不同座標；不要把每日額度視為可在第一天一次用完。已有相同精確座標可用快取；若只處理 50,000 張含 GPS 照片，呼叫上限自然降低。實際命中率不猜測。

官方允許地址資料保存；仍需保留 Geoapify 與資料來源署名。原型在詳情提供連結，面板署名如何安排尚需依媒介要求確認，不將免費方案視為已完成正式發布條件。全球或偏遠地點可能沒有完整地址；保留提供者回傳的地址及精度，不讓模型補造。

OSMF 公共 Nominatim **不作為 10 萬張後台查詢預設服務**。其[使用政策](https://operations.osmfoundation.org/policies/nominatim/)限制最高每秒 1 次、大量查詢不鼓勵，長於一天／週期任務每分鐘 4 次且必須快取；不能將它當免費大批次捷徑。本分支沒有呼叫該公共 API。自架全球 Nominatim 的資料／RAM 開銷不適合直接加到目前 NAS；若只要行政區，可另評估離線行政區 polygon，但仍不能提供完整街道門牌。

更省錢的正式實作順序：既有照片僅查／快取地址 → 顯示行政區 → 新照片同次分析帶地標 → 已知旅遊樣本人工評估後才決定是否回填舊照片地標。將地圖網路工作放在獨立、有持久每日額度與中斷續跑的低併發工作，不阻塞照片分析或裝置渲染。這個批次工作尚未實作；目前 `enrich` 僅為明確指定單照片的工具。

## 最新模型研究：2026-10-22 查詢結果

同時讀取 OpenRouter 公開模型與 endpoint API，快照見 [OpenRouter shortlist](openrouter-shortlist-20261022.json)。API 的 `supported_parameters` 與 `reasoning` 比單看最低價更重要；同模型不同供應端的價格、量化、Schema 支援與可用性不同。網站與 API 價格不一致時保留差異，不拼湊不同供應端的最低輸入／最低輸出成不存在的費率。

- **GPT-6 Luna**：保留作為首選基準。圖片、Structured Outputs、`none` 推理均有官方資料；本次查到最低可用 Standard 路徑為 $0.10/$0.50。OpenAI Flex 雖 $0.05/$0.25，但本次 endpoint 狀態為 -5，不能將它當成目前可用路徑，也不能把 OpenAI Batch 折扣直接套到目前 OpenRouter 請求。
- **Nex-N2.5-Mini**：$0.025/$0.10，目錄標示圖片、Structured Outputs、可關推理。值得小樣本比較，主打 agent／coding 的公開說明不足以證明台灣地標與短句品質，因此不直接改成預設。
- **DeepSeek V4.1 Flash**：原生圖片、可關推理；Baidu FP8 當時促銷價 $0.0342/$0.1368，僅作有期限／路徑條件的候選。目錄廣告最低價 $0.02394/$0.375 是另一個 OpenInference FP4 路徑。量化、供應端、促銷都要固定並記錄，不能由 general benchmarks 推定照片任務更好。
- **Qwen3.7 Flash**：$0.03/$0.13 看似省錢，但本次所有 endpoint 均沒有 `structured_outputs`，官方頁寫明只有 JSON output、沒有 JSON Schema enforcement。對目前包含安全分類與方向欄位的嚴格回應，不作直接替代預設。
- **GLM 5.3 Flash**：目錄最低 $0.041191/$0.12、頁面／可用 Strict 路徑見不同較高輸入價；模型目錄標示 `mandatory=true`、僅 low/high/max，不能接受本專案的 `none`。不以低表價宣稱整批較省。
- **Gemini 2.5 Flash-Lite**：$0.10/$0.40，只比 Luna 的地標增量少 $0.19／10 萬張；不為這個差額直接換回較舊模型。新 Gemini 3.5 Flash-Lite 目錄價 $0.30/$2.50，Gemini 3.1 Flash-Lite $0.25/$1.50，均未比 Luna 更便宜。

**沒有任何本次找到的資料直接比較這些模型在「台灣照片、知名地標、繁中 20 字短句、安全排除、EXIF 後方向」的品質。** 廠商介紹、程式／數學榜單或使用量不能證明品質相同。建議保留 Luna，再以 60 張人工標註照片比較 Luna／Nex／DeepSeek：20 張明確地標、20 張容易誤認地點、20 張普通生活／無 GPS，含不同方向與安全排除案例；核對地標誤報、拒答留白、Schema 完整性、短句品質、方向與安全分類、推理 Token／實際費用。測試費用預算可先設 US$1；本次未建立付費測試或自動上傳照片。

## 重現工具與驗證邊界

在 Python 3.12 開發環境安裝專案依賴；Token probe 額外需要開發用 `tiktoken`，未加入正式 runtime requirements。

```sh
python scripts/experiments/photo_places.py probe --output-dir /tmp/new-place-probe
python scripts/experiments/preview_frame_places.py --output-dir /tmp/frame-place-preview
python scripts/experiments/photo_places.py build-request --body /tmp/v6-body.json --output /tmp/place-request.json --region-hint 新店區
```

工具不送出模型請求，輸出檔含圖片或地址時視為使用者私人資料。請在已遷移的測試資料庫副本操作：

```sh
python scripts/experiments/photo_places.py enrich --database /tmp/test-copy.db --photo-id PHOTO_ID
# 預設 dry-run；快取未命中且明確要查詢時，再使用 --write --allow-network。
# API Key 從 GEOCODING_API_KEY 讀取，不放命令參數。
python scripts/experiments/photo_places.py import-landmark --database /tmp/test-copy.db --photo-id PHOTO_ID --expected-sha256 PHOTO_SHA256 --latitude 25.033 --longitude 121.5654 --result /tmp/result.json
# 預設驗證而不寫入；明確要匯入再加 --write。無 GPS 時不帶 latitude/longitude。
```

本地已執行離線請求／Token probe、四種示意生成、Python syntax、changed-owner lint、`git diff --check` 與文件契約檢查。新增 unit／integration／migration 測試交由 Hosted CI；不在本地執行 pytest。測試涵蓋 GPS 範圍、相鄰座標不共享門牌、低信心退回行政區、同次圖片請求、舊 v6 Schema 不變、地址／地標分離、內容變更失效、無 GPS 地標與 API Key 錯誤遮蔽。

尚未完成：實際地址提供者覆蓋／準確率、付費模型品質比較、正式 Worker／Batch 整合、10 萬張真實 NAS 資源與面板效果驗收。未遷移正式資料庫、未部署、未更換正式模型、未刷韌體。

### 09:00 交接檢查

2026-10-22 早上只檢查一次 PR #168 的當時最新提交 `13f8fe7`：[Hosted CI #1245](https://github.com/steven87090799/InkTime/actions/runs/39641894317) 失敗，容器安全／Benchmark 契約通過；Migration、靜態檢查、Web E2E 也通過，但不能因此宣稱全部測試成功。

兩個明確失敗已修正：舊 Schema 70 回歸測試的升級清單改為包含 69 之後所有已知 Migration；新 `test_photo_places.py` 整合測試登記到 render／persistence／web API 的 CI owner 與 runner。另將地點 unit 測試加入 scanner／provider owner，避免未來單獨更動相關來源時漏跑。本地只做語法、Lint、文件與測試路由靜態驗證，不跑 pytest。修正推送後的新 CI 尚待確認（`CI_STATUS=CI_PENDING`）；本次交接不輪詢或重跑舊 CI。

本次一次性早上交接完成後刪除提醒排程，不建立後續監控。

## 一手來源

- [OpenRouter GPT-6 Luna](https://openrouter.ai/openai/gpt-6-luna)、[模型目錄 API](https://openrouter.ai/api/v1/models)、[Luna endpoints](https://openrouter.ai/api/v1/models/openai/gpt-6-luna-20260922/endpoints)
- [OpenAI GPT-6 Luna 文件](https://developers.openai.com/api/docs/models/gpt-6-luna)、[OpenAI pricing](https://developers.openai.com/api/docs/pricing)
- [OpenRouter Nex-N2.5-Mini](https://openrouter.ai/nex-agi/nex-n2.5-mini)、[DeepSeek V4.1 Flash](https://openrouter.ai/deepseek/deepseek-v4.1-flash)
- [OpenRouter Qwen3.7 Flash](https://openrouter.ai/qwen/qwen3.7-flash)、[GLM 5.3 Flash](https://openrouter.ai/z-ai/glm-5.3-flash)、[Gemini 2.5 Flash-Lite](https://openrouter.ai/google/gemini-2.5-flash-lite)
- [OpenRouter Structured Outputs](https://openrouter.ai/docs/guides/features/structured-outputs)、[Reasoning Tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
- [Geoapify pricing](https://www.geoapify.com/pricing/)、[Reverse API](https://apidocs.geoapify.com/docs/geocoding/reverse-geocoding/)、[保存資料說明](https://www.geoapify.com/geocoding-api/)
- [OSMF Nominatim 使用政策](https://operations.osmfoundation.org/policies/nominatim/)、[Reverse 精度說明](https://nominatim.org/release-docs/latest/api/Reverse/)
