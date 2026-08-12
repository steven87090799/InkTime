# 合併後實機與外部服務驗收邊界

本文件把 software CI、hosted compile、simulator/mock 與真實外部環境分開。當前報告狀態如下：

## 2.8 合併基線

| 項目 | 值 |
|---|---|
| 整合 PR | PR #62 |
| Firmware | `2.8.0` |
| Database Migration | `36` |
| ESP32 Config Store | 目前 schema `4`；相容讀取 schema `1`、`2`、`3` |

| 項目 | 狀態 | 目前可用證據 |
|---|---|---|
| Python／SQLite／Batch software | PASS | local pytest、migration、Batch lifecycle／provider focused tests |
| GitHub hosted CI | PASS | PR #62 exact-head routed／full checks 全數成功 |
| Compose／TLS／runtime soak／Playwright | PASS | hosted workflow matrix |
| ESP32 cross-compile | PASS | hosted `esp32-compile` |
| 真實 OpenAI API / Batch | NOT RUN | 沒有使用 production key，也沒有上傳真實照片 |
| 真實 NAS volume | NOT RUN | 未連接 production NAS；Compose 只驗證容器邊界 |
| 真實 ESP32／PhotoPainter／電子紙 | NOT RUN | 已對照官方原始碼；沒有實際裝置畫面結果 |

## 真實 OpenAI Batch 人工 smoke

由具備 API key 權限的人員在隔離環境執行，使用 1–3 張非敏感 JPEG；不要把 key、原圖、response 或 token 放進 commit、log、截圖或 issue。

1. 啟用 live smoke 的明確開關，設定 key、base URL（如有）與測試圖片路徑。
2. 執行 `scripts/openai_batch_live_smoke.py`，確認實際 provider identity、model、request/batch ID、image input、full JSON Schema、reasoning/token usage、成本與 Input/Output/Error File。
3. 確認三種遠端檔案在成功、部分失敗、取消／過期與未知 delete result 下都能清理；cleanup retry 不得改寫既有 terminal semantic。
4. 先用管理介面的 100-photo sample，確認 SQLite item mapping、亂序回填、missing/stale/schema-invalid 統計與重啟恢復；通過後才考慮 500-photo sample 或 `all_eligible_missing_analysis`。
5. 任何未執行、不可重現或使用 mock 的步驟都必須保留 `NOT RUN`，不能寫成 PASS。

## 真實 NAS persistence

1. 以 production-like NAS mount 執行 backup、migration、restart、rollback、disk-full 與 permission-denied scenario。
2. 確認 SQLite、`/data/batches`、backup、queue 與 log rotation 位於正確持久化邊界；檢查 NAS snapshot/restore 與 volume ownership。
3. 以真實 NAS 完成一次 graceful restart 與一次故障中斷後，核對 batch lease、cleanup state、provider identity、usage/cost 與 no-upload/no-display privacy flags。

在上述步驟完成前，Compose persistence 只能標記為 software PASS，NAS production acceptance 必須保持 `NOT RUN`。

## ESP32／PhotoPainter 證據邊界

PhotoPainter Profile 的 GPIO、I²C、SD、音訊與面板命令以 Waveshare 官方原始碼固定，
Hosted `esp32-compile` 負責阻止接線或建置設定漂移。能源遙測只供診斷，不需要使用者
量測電流或電壓，也不會成為刷新條件。沒有實際裝置時，真實畫面仍保持 `NOT RUN`；
燒錄後只需確認配對、正式圖片顯示、方向與下一次排程喚醒，不要求外接量測儀器。
