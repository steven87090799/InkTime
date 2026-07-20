# 裝置可靠性與六／七色渲染指南

本指南涵蓋 InkTime 的 GDEP073E01 六色、GDEY073D46 七色發布、抖動算法、裝置設定版本 ACK、離線／恢復通知與 Webhook。硬體接線、電源與燒錄步驟仍以 [ESP32 指南](ESP32_GUIDE_ZH_TW.md)為準。

## 1. Profile 與線上格式

| Profile key | 面板／用途 | 色盤代碼 | 格式 | 480×800 大小 |
|---|---|---|---|---:|
| `safe_4c` | 舊版相容；黑白紅黃 | 0 黑、1 白、2 紅、3 黃 | 2bpp，每 byte 4 pixels | 96,000 bytes |
| `gdep073e01_6c` | GDEP073E01 Spectra 6 | 0 黑、1 白、2 綠、3 藍、4 紅、5 黃 | indexed4，每 byte 2 pixels | 192,000 bytes |
| `gdey073d46_7c` | GDEY073D46 ACeP | 上述六色＋6 橘 | indexed4，每 byte 2 pixels | 192,000 bytes |

indexed4 採 GxEPD2 的邏輯顏色順序。偶數 pixel 放在高 4 bits、奇數 pixel 放在低 4 bits。伺服器 Manifest schema v2 會包含 `render_profile`、`pixel_format`、完整 `palette`、抖動參數、檔案大小與 SHA-256。韌體只在尺寸、Profile、長度與雜湊全部相符時刷新。

每個 Profile 有獨立的 `latest.<profile>` 原子指標，因此同一個 InkTime 可以同時服務不同面板。渲染頁的「發布全部 Profile」會為同一批照片建立三個獨立版本；回滾只影響該版本所屬 Profile，不會把其他面板一起回滾。

## 2. 抖動算法怎麼選

設定位置：Web「設定」→「渲染設定」。渲染頁的實際色盤預覽是量化後 PNG，不是原始 RGB 圖。

| `render.dither` | 特性 | 適合 | 成本／注意 |
|---|---|---|---|
| `photo_smooth` | 3×3 中值濾波後套用原廠固定色盤 Floyd–Steinberg | GDEP 人像與壓縮照片；減少色塊和 JPEG／感光雜點 | 可能略微柔化極細線條；建議先用實機 A/B |
| `gooddisplay` | 重現 Good Display 固定色盤與 Pillow Floyd–Steinberg | 與原廠轉換工具逐像素比較 | 強度固定；GDEP 使用原廠純色工作色盤 |
| `floyd_steinberg` | 蛇行誤差擴散，漸層與照片自然 | 人像、風景；預設 | 發布時 CPU 最高，結果固定 |
| `atkinson` | 擴散較少，亮部與細節清楚 | 文字混合照片、復古風格 | 可能犧牲暗部層次 |
| `bayer8` | 8×8 有序抖動，紋理較細 | N100 快速批次、規律漸層 | 可能看見規則網點 |
| `bayer4` | 4×4 有序抖動，顆粒較明顯 | 小字、圖示、強對比版面 | 網點比 Bayer 8 明顯 |
| `none` | 直接映射最近色 | 已先做平面色設計的圖示／海報 | 照片容易色階斷裂 |

- `render.color_distance=oklab`：依人眼感知找最近色，建議正式使用。
- `render.color_distance=rgb`：與舊式 RGB 歐氏距離較接近，適合相容比較。
- `render.dither_strength`：0～2；1 是標準。建議先在 0.7～1.2 調整，過高會出現不必要色點。
- `gooddisplay` 與 `photo_smooth` 固定採原廠標準 Floyd–Steinberg，不使用強度滑桿。
- GDEP 的 `gooddisplay`／`photo_smooth` 使用原廠純色作為量化工作色盤與 PNG 預覽；Manifest 會攜帶同一份色盤，虛擬接收端不會再解碼成另一組顏色。
- 抖動只在 Worker 正式發布時執行，不會讓 Web／Scheduler 待機持續耗 CPU。查表快取固定為每個 Profile 約 32 KiB，誤差擴散只保留 480-pixel 的兩至三列誤差，不建立整張浮點誤差圖。

色盤 RGB 是量化目標與螢幕預覽近似值，不是面板的色彩校正保證。電子紙實際顏色會受批次、溫度、老化、驅動 waveform、環境光與 adapter 供電影響；正式照片牆應以同批面板印出測試圖，再微調 Profile 目標值。

## 3. 裝置設定版本 ACK

1. 每台裝置建立時期望版本為 `config_version=1`、ACK 為 0。
2. 在裝置頁修改時區、每日刷新時間、旋轉或面板 Profile，伺服器將期望版本加一。只改名稱或啟停不會製造無意義版本。
3. Manifest 的 `device_config.schema_version=2` 帶期望版本與面板 Profile。
4. 韌體驗證版本不得倒退、Profile 必須是 `safe_4c` 或與編譯面板相同；驗證成功後寫入 NVS。
5. 狀態回報帶 `applied_config_version`。伺服器只接受「高於目前 ACK 且不高於期望版本」的值，避免錯誤韌體跳過尚未套用的設定。
6. 裝置頁顯示「期望 vN／裝置 vM」。兩者相同才標示已 ACK，並顯示伺服器收到 ACK 的時間。

若 Profile 不符，韌體回報 `DEVICE-CONFIG-PROFILE` 且不刷新。請確認實際面板、Arduino compile flag、Web 裝置 Profile 與已發布 Profile 四者一致。不要為了消除警告而把錯誤型號改成 `safe_4c`；`safe_4c` 只保證資料色盤相容，不會把錯誤的面板 driver class 變正確。

## 4. 離線、提醒與恢復

Scheduler 預設每 300 秒掃描一次，不為每次掃描輸出 INFO Log。啟用裝置最後狀態、最後 Manifest 驗證時間或建立時間超過 `notification.device_offline_hours`（預設 30 小時）才轉為離線。

- 首次離線：建立一筆 warning 裝置事件與站內通知，設置 `offline_alert_active=1`。
- 持續離線：預設不重複通知。只有開啟 `device_offline_repeat_enabled` 才依 `device_offline_cooldown_hours` 再提醒。
- 恢復：裝置再次以有效 Bearer Token 連線或回報狀態後，清除離線旗標；若啟用恢復通知則建立一筆 recovery。
- 停用裝置不參與離線判定；新裝置也必須超過完整門檻才會通知。
- 所有狀態與 Webhook 嘗試持久化在 SQLite，容器重新啟動不會遺失節流狀態。

每日刷新裝置建議門檻至少 26～30 小時，以容納 Wi-Fi、NTP、排程與短暫停機偏差。若改成每週刷新，必須同步調高離線門檻，否則是設定造成的預期誤報。

## 5. Webhook

1. Web「設定」→「裝置通知」填完整 `http://` 或 `https://` URL 並啟用 Webhook。
2. 若接收端需要 Bearer Token，在同頁「Webhook 認證 Token」輸入；它以平台主密鑰加密保存在 `secrets`，不進一般設定歷史或 Log。
3. 按「傳送測試通知」。HTTP 2xx 視為成功。
4. 失敗會在 60 秒、再 300 秒後重試；第三次仍失敗後標記 `failed`，不無限重送。

Payload 範例：

```json
{
  "schema_version": 1,
  "notification_id": 42,
  "kind": "offline",
  "level": "warning",
  "title": "InkTime 裝置離線",
  "message": "客廳電子紙已超過 30 小時未連線……",
  "device": {"id": "uuid", "name": "客廳電子紙"},
  "details": {"last_contact_at": "2026-07-17T00:00:00+00:00", "threshold_hours": 30},
  "created_at": "2026-07-18T06:00:00+00:00"
}
```

Webhook URL 是 administrator 級設定，允許可信內網端點；這也代表管理員可以要求容器連到內網服務。不要把管理員權限交給不可信帳號。正式跨網路傳輸應使用 HTTPS；Token 不會放進 payload，只會放在 `Authorization: Bearer` Header。

## 6. 資源與容量

- 四色 payload：96,000 bytes；六／七色：192,000 bytes。韌體直接保存壓縮索引，不再展開舊版 384,000-byte 每像素 framebuffer。
- GxEPD2 仍使用分頁顯示 buffer；畫面刷新時韌體逐 pixel 解碼，完整刷新時間主要由面板 waveform 決定。
- 五張七色發布檔約 0.92 MiB，另加五張 PNG 預覽；三 Profile 全發布時約為這個量的三倍加預覽。請把 `/data/releases` 納入磁碟監控與備份容量估算。
- Worker 發布是低頻 CPU 工作；若 N100 同時在做模型分析，可先暫停分析工作或改用 `bayer8`，避免兩個 CPU 密集工作同時競爭。
- Scheduler 每 5 分鐘只做索引式 SQLite 查詢；沒有轉態時不送網路、不建立通知、不輸出事件 Log。

## 7. 升級與回滾順序

建議順序：先備份資料庫與 `/data/releases` → 更新三個 Docker 服務並完成 migration v7 → 為每個實際面板發布對應 Profile → 燒錄 2.2.0 韌體 → 在裝置頁改成正確 Profile → 等待 ACK。

舊 2.1.0 韌體只認 schema v1／2bpp。升級期間可把裝置留在 `safe_4c` 並發布四色版本；不要先把它切到六／七色。若新韌體異常，先將裝置 Profile 回到 `safe_4c`、發布四色並回滾韌體；設定版本會再次增加，需等待舊／相容韌體 ACK。

實際面板驗收至少包含：純色塊、RGB 漸層、人像膚色、細字、弱 Wi-Fi 下載中斷、SHA 錯誤、Profile 不符、NVS 斷電恢復、一次離線與一次恢復通知。軟體測試與 Arduino 編譯不能代替真實面板的顏色、供電、BUSY 與溫度驗收。
