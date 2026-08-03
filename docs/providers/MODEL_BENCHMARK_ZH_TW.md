# 模型 Benchmark：離線預設、有限 Live 與不污染正式資料

入口是 [`scripts/benchmark_models.py`](../../scripts/benchmark_models.py)，服務實作是 [`inktime/app/services/model_benchmark.py`](../../inktime/app/services/model_benchmark.py)。Benchmark 的目的，是比較 request contract、Token／成本、延遲與結果契約；它不是「準確率」宣稱，也不把舊版 `smart_two_stage` 假裝成另一種現行流程。

## 預設離線模式

```bash
python scripts/benchmark_models.py
```

未加 `--live` 時：

- 只產生 deterministic synthetic JPEG fixture，不讀取私人相簿，不呼叫 HTTP、`/models` 或任何外部 Provider。
- 只建構與正式分析相同的 request body，量測 512／1024／1600、prompt profile、caption variants、reasoning 與 OpenRouter options 的 request／schema／image bytes。
- `network_invocations=0`、`production_mutations=0` 是必要輸出；離線結果的 schema success 不會冒充模型品質。
- 預設輸出到被 Git 忽略的 `data/benchmarks/latest.json` 與 `data/benchmarks/latest.md`；CI 會把 synthetic sample 報告上傳為 artifact。

可調整範例：

```bash
python scripts/benchmark_models.py \
  --models offline-synthetic \
  --sample-count 10 \
  --image-sides 512,1024,1600 \
  --prompt-profiles default,advanced \
  --variants off,on \
  --reasoning none,low \
  --seed inktime-benchmark-v1 \
  --output data/benchmarks/run.json \
  --markdown-output data/benchmarks/run.md
```

axes 上限為 96 組，sample 上限為 100 張；這是 bounded memory／CI 時間契約，不是生產照片庫處理上限。

## 比較軸與指標

現行 single contract 應比較下列 axes：

- Provider／model 完整 ID。
- 圖片最長邊 512、1024、1600；512 不會被自動升成 1024。
- `default`／`advanced` prompt profile。
- `caption_variants` 開／關；候選仍須在同一次圖片 request 產生。
- reasoning `none`／`low`；OpenRouter 使用 `reasoning.effort`。
- OpenRouter routing options，例如 `only`、`ignore`、`sort`、`data_collection`、`zdr`。

每組報告至少包含：

`total_photos`、`provider_requests`、`vision_requests`、`repair_requests`、`success_count`、`schema_success_rate`、`first_pass_schema_success_rate`、`repair_rate`、`failure_rate`、`input_tokens`、`cached_tokens`、`cache_write_tokens`、`uncached_tokens`、`output_tokens`、`reasoning_tokens`、`estimated_cost`、`provider_reported_cost`、`actual_cost`、`unknown_cost_count`、`avg_cost_per_photo`、`cost_per_1000_photos`、`avg_latency_ms`、`p50_latency_ms`、`p95_latency_ms`、`avg_request_body_bytes`、`avg_image_bytes`、`avg_system_prompt_chars` 與 `avg_schema_chars`。

沒有 baseline 時，不輸出 accuracy。若未來加入 golden／人工標記資料，只能使用「相對一致性」名稱，例如 score／grade、type、keep、orientation agreement、caption length；排名則報告樣本安全縮小 K 後的 Top 10／25／50 overlap 與 Spearman。

## Live 模式：必須明確開啟

Live benchmark 不在 CI，也不應在未核准的正式照片庫上執行：

```bash
INKTIME_BENCHMARK_API_KEY='只在受控 shell 注入' \
python scripts/benchmark_models.py \
  --live \
  --provider openrouter \
  --models openai/<完整模型 ID> \
  --sample-count 20 \
  --seed inktime-benchmark-v1 \
  --max-requests 40 \
  --max-cost 1.00 \
  --output data/benchmarks/live.json \
  --markdown-output data/benchmarks/live.md
```

目前 CLI 的 live adapter 使用 deterministic synthetic fixture，避免把私人圖片誤當成 benchmark dataset；正式相簿若要加入，必須另接具備 `never_upload`、active、eligible、missing、manual exclude 過濾與 `hash(seed + photo_id)` 排序的唯讀資料集 adapter，不能直接繞過資料邊界。

Live 安全條件：

- `max_requests` 最多 100，並同時受 `max-cost` 停止線約束。
- 成本回報未知時增加 `unknown_cost_count`；不得以零來掩蓋未知帳務。
- JSON 修復最多一次且只傳文字，不會第二次上傳圖片。
- 只寫明確指定的 JSON／Markdown artifact，不寫 `photo_analysis`、`releases`、`display_history` 或正式 AI Cache。
- `stopped_by_budget=true` 時，報告應被視為部分樣本，不可外推全庫。

真實 OpenRouter live benchmark、付費成本與正式品質結論在本分支維持 `NOT RUN`。
