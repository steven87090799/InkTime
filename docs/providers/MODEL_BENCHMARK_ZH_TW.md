# 模型 Benchmark：離線預設、有限 Live 與不污染正式資料

入口是 [`scripts/benchmark_models.py`](../../scripts/benchmark_models.py)，服務實作是 [`inktime/app/services/model_benchmark.py`](../../inktime/app/services/model_benchmark.py)，純指標位於 [`inktime/app/services/benchmark_metrics.py`](../../inktime/app/services/benchmark_metrics.py)。Benchmark 明確分為 Contract Benchmark 與 Quality／Ranking Benchmark；它不是沒有 golden data 時的「準確率」宣稱，也不把舊版 `smart_two_stage` 假裝成另一種現行流程。

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

offline JSON 的 `mode` 是 `offline-contract`，並回傳 `contract_metrics`；`quality_metrics` 與 `ranking_metrics` 必須是 `null`。CI 只驗證 request body 與 metric calculator contract，不代表真實模型品質。

每組 Contract 報告至少包含：

`total_photos`、`provider_requests`、`vision_requests`、`repair_requests`、`success_count`、`schema_success_rate`、`first_pass_schema_success_rate`、`repair_rate`、`failure_rate`、`input_tokens`、`cached_tokens`、`cache_write_tokens`、`uncached_tokens`、`output_tokens`、`reasoning_tokens`、`estimated_cost`、`provider_reported_cost`、`actual_cost`、`unknown_cost_count`、`avg_cost_per_photo`、`cost_per_1000_photos`、`avg_latency_ms`、`p50_latency_ms`、`p95_latency_ms`、`avg_request_body_bytes`、`avg_image_bytes`、`avg_system_prompt_chars` 與 `avg_schema_chars`。

Quality metrics 使用 [`benchmarks/golden/manifest.schema.json`](../../benchmarks/golden/manifest.schema.json) 定義的 non-private golden manifest。Manifest 的 canonical exclusion 欄位是 `inactive`、`ineligible`、`missing`、`never_upload` 與 `manually_excluded`；任何未知欄位、錯誤型別、非 `non_private` privacy、path traversal 或禁止目錄路徑都會 fail-closed，而且在任何 Provider network request 前排除。Grade 使用 `E=0` 到 `S=5`，回報 `exact_grade_accuracy`、`within_one_grade_accuracy` 與 `mean_absolute_grade_distance`；types 回報 micro precision／recall／F1 與 Jaccard；`should_keep` 回報 accuracy／precision／recall／F1；orientation 回報只針對 expected non-ambiguous 的 `rotation_exact_accuracy`、ambiguous rate 與 `false_confident_orientation_rate`。Ranking 直接重用 production `calculate_ranking_score()`；manifest 的 `expected_score`／`expected_rank` 必須代表同一套 production ranking。報告固定輸出 `ranking_rule_version`、`ranking_weights` 與 favorite bonus policy；Top-K 使用 deterministic exact-K membership，tie 不會使 overlap rate 超過 1，資料集小於 K 時使用 `effective_k=min(K,dataset_size)`。

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
  --dataset path/to/approved-golden-manifest.json \
  --max-requests 40 \
  --max-cost 1.00 \
  --confirm-live-quality \
  --output data/benchmarks/live.json \
  --markdown-output data/benchmarks/live.md
```

Live quality 只接受明確的 non-private golden manifest；manifest 內的圖片必須位於 manifest 目錄內，且路徑不得落入 production photo、cache、release 或使用者圖片目錄。若要使用管理員提供的資料，必須先完成專用 benchmark export，再以 `--dataset` 明確指定，不能直接掃正式相簿。

Live 安全條件：

- `max_requests` 最多 100，並同時受 `max-cost` bounded post-response stop 約束；`max_cost` 是收到上一個 response 後的累計停止線，不是數學上的絕對 pre-request ceiling。
- `--live`、`--api-key`、`--dataset`、`--confirm-live-quality`、`--max-requests`、`--max-cost` 與 `--sample-count` 都是明確的安全邊界。
- 成本回報未知時增加 `unknown_cost_count` 並停止後續 Provider request；不得以零來掩蓋未知帳務。
- JSON 修復最多一次且只傳文字，不會第二次上傳圖片。
- 只寫明確指定的 JSON／Markdown artifact，不寫 `photo_analysis`、`releases`、`display_history` 或正式 AI Cache。
- `stopped_by_budget=true` 時，報告應被視為部分樣本，不可外推全庫。

JSON 會把 `contract_metrics`、`quality_metrics`、`ranking_metrics` 分開；`live-quality` 的品質結果只代表該次明確 dataset 與 bounded sample，不可外推全庫。真實 OpenRouter live benchmark、付費成本與正式品質結論在本分支維持 `NOT RUN`。
