# 圖片來源、預覽與 AI 輸入 contract

唯一來源定義：`inktime/app/domain/photos/formats.py`。Scanner、預處理、縮圖、
Web/Review、scoring upload、render upload、正式 RenderService 與 benchmark fixture
都使用同一套來源開啟／尺寸政策。Release/preview PNG 與內部 render cache 是已產生的
有界輸出，不是原圖入口。

| 輸入 | 支援狀態 |
| --- | --- |
| JPEG/JPG、PNG、WEBP、HEIC/HEIF、TIFF/TIF、BMP | 正式支援，第一張／HEIF primary still |
| iPhone 48MP HEIC/JPEG | 支援，包括 8064×6048（48.8MP）；不必改 iPhone 設定 |
| iPhone ProRAW `.DNG` | **尚未支援**。現有 Pillow 並非可靠 ProRAW decoder，未加入 rawpy/libraw；scanner 計入 `unsupported_raw` 並提示，不當成 corrupt photo |
| Live Photo | HEIC/JPEG still 正常掃描；同 basename 的 MOV 計入 video exclusion |
| GIF | 維持 excluded/video-like；不因 Pillow 可開啟而視為支援圖片 |

原始 `/photos` 一律 read-only。程式不轉存全尺寸 JPEG、不覆寫、重新編碼或修改
來源 EXIF。只建立需求尺寸的 lazy derivative；沒有全庫 migration 或啟動重建。

## 大圖與記憶體

共同硬上限為 **60,000,000 pixels、12,000px 任一邊、200MiB 檔案**。
Scanner 設定只能進一步收緊；舊 DB 若存了更大的 scanner 上限，執行時仍會 clamp。
Upload 的既有 25MiB HTTP 傳輸限制仍保留，這不是 NAS 圖片的 200MiB decode 上限。
Pillow `MAX_IMAGE_PIXELS`、DecompressionBomb 防護與 libheif security limits 不關閉。

JPEG 在任何 pixel load 前先 `draft()`，縮圖不先完整解碼 48MP JPEG。
**固定版本 pillow-heif 1.5.0 的 Pillow plugin 沒有 decoder-level draft/downsample API**；
`info['thumbnails']` 只提供 embedded thumbnail 尺寸，不能當成可取用且可信的完整預覽。
HEIF、PNG、TIFF、BMP 等無 draft 的格式仍可能完整解碼，不能把 `thumbnail()` 說成有界解碼。

因此全部來源開啟（含 HEIF header/metadata 配置）以同一個 `/data/.inktime-image-decode-<uid>/source.lock`
跨 process/container 序列化。正式 Web/Worker/Scheduler 必須共用 `/data`、同 UID，並使用
支援 flock 的本地資料磁碟。非 container 工具預設使用該使用者的暫存目錄鎖；鎖目錄為該 UID 專用 0700，拒絕 symlink／不安全目錄。
等待超過 30 秒回 `IMG-DECODE-BUSY`，不額外生成 process；一張大圖不會在平行 workers
同時多份展開。此鎖與 content cache shard lock 分工，cache hit 不需來源解碼鎖。

HEIF decode_threads=1，停用不使用的 depth/aux/thumbnail metadata 收集。
縮小到需求尺寸後才 EXIF transpose、RGB/alpha compositing；不得在縮小前複製全尺寸 RGB。
Web/AI working edge 分別 512/1024/1600，render 最多 3200px，以保留組版空間。
這是可預測的併發／輸入界線，**不是所有 codec 嚴格的 RSS 保證**；解碼函式本身沒有強制 wall-clock kill。

Compose 的 Web/Worker/Scheduler 記憶體上限統一至少 1GiB（limit 不是預先保留配置）。
原 Web 384MiB 不足以處理合法 HEIF。不要用較低的環境變數覆寫後宣稱支援 48MP。
容器基礎服務與既有 render/AI 工作仍需額外主機餘裕；不建議增加 Web process 數。

本次本機 ARM64/Pillow 12.3.0/pillow-heif 1.5.0 的純色 **48.8MP RGB HEIF grid**
驗證：preprocess + 512/1024/1600 derivative + render load 峰值 RSS **574,799,872 bytes（548MiB）**，
用時 2.30 秒。這是合成圖的本機量測，不代表所有 iPhone/HDR 檔案、NAS 或真實面板驗收。
一般來源仍可能占數百 MiB；檔案上限與序列鎖不能取代容器餘裕。

## Browser / AI / orientation

| 用途 | URL／入口 | 輸出 |
| --- | --- | --- |
| 照片列表、AI Trace 列表 | `/api/v1/photos/<id>/thumbnail` | 最長邊 512px RGB JPEG |
| 照片詳細頁、AI Trace 詳細頁 | `/api/v1/photos/<id>/preview` | 最長邊 1600px RGB JPEG |
| Review | 原有 Review thumbnail URL | 同一個 512px JPEG cache |
| AI（single 與 batch） | `ThumbnailCache.acquire_for_use` | 設定 `analysis.image_max_side`，通常 1024px、JPEG quality 88 |
| 舊 original API | `/api/v1/photos/<id>/image` | **保留原始 bytes 語意**；不作為 HTML `<img>` 來源 |

所有瀏覽器 derivative 回 `Content-Type: image/jpeg`；Vision payload 使用 `.jpg` 的
`data:image/jpeg;base64,...`，從不傳 raw HEIC。Provider 的一般 PNG/WEBP 輸入相容性保留，
但不再把未知 suffix（包含 HEIC）預設冒充 JPEG。

EXIF 方向只套用一次。HEIF container orientation 已由 libheif 解碼套用，plugin 會清除
EXIF Orientation；`original_orientation` 僅作 metadata 稽核，不能再次旋轉像素。
Pillow 12.3 TIFF 同樣在開啟時回 oriented dimensions；DB 不可再交換寬高。
輸出 JPEG 清除 EXIF/XMP/GPS 與舊方向；RGBA/LA/P 透明度合成白底，CMYK 轉 RGB，
16-bit grayscale 線性縮到 8-bit，HEIF HDR 使用 plugin 的 8-bit decode。沒有新增 ICC/HDR tone-mapping pipeline。

## Cache、安全與 diagnostics

新 derivative 使用 `<sha256>-v2-<size>.jpg`。保留舊 JPEG cache，不原地改寫；首次需求才生成新版。
舊檔仍可由原 retention/capacity maintenance 處理，沒有新 O(N) request/startup inventory。
保留 sharded single-flight、fsync、temporary file、atomic replace、來源 SHA 重算與尺寸驗證，
並完整 load 小 JPEG 檢查 truncated pixel data。失敗清掉暫存檔。
所有 library HTTP/render path 保留 `safe_join`；來源 open 拒絕 symlink/non-regular file，
並檢查開啟前後 inode/mtime/ctime/size；preprocessor 從同一 descriptor 計算 SHA。EXIF JSON 原有 64KiB 上限不變。

Vision preprocessing version 更新為 v3、render cache version 更新為 image-v2，使新的工作
不誤用舊像素語意。歷史 AI cache、analysis、usage、release、photo IDs 與 schema 不變；
不自動重新分析、重建 release 或清除資料。已凍結的舊 plan/歷史記錄仍可讀；執行時只升級 plan 副本的 preprocessing version，不改寫歷史快照。

| error_code | HTTP | 意義 |
| --- | --- | --- |
| IMG-MISSING | 404 | 原圖或 photo record 不存在 |
| IMG-UNSUPPORTED | 415 | 不支援來源，含 DNG |
| IMG-HEIF-UNAVAILABLE | 503 | HEVC decoder 缺失／不可用 |
| IMG-CORRUPT | 422 | 無法識別或損壞的圖片 |
| THUMB-004 | 409 | 來源在產生期間或掃描後改變 |
| THUMB-005 | 413 | pixels／edge／decompression bomb 防線 |
| IMG-FILE-LIMIT | 413 | 來源檔案超過上限 |
| IMG-IO | 403/503 | 權限或 I/O 問題（非法路徑 photos API 404、Review 維持 400） |
| IMG-DECODE-BUSY | 503 | 全域解碼槽等待超時 |
| THUMB-001/003 | 503 | cache I/O／輸出驗證失敗（保留舊 contract） |

失敗回 JSON `error_code/message/stage`；UI 顯示「縮圖無法產生」，不留無限 spinner。
log 依 stage/code 限速，包含 photo_id/format，不寫原始路徑、GPS、照片或 Provider key。
scanner 保存 masked-path 錯誤與 aggregate；正常照片不逐張 INFO。
損壞／不支援／來源已變更／安全上限／decoder 缺失為 terminal queue failure，需修復來源或設定後再提交；decode busy／暫時 I/O 保留 bounded retry。
`/health/ready` 檢查 HEIF decoder，`/health/detail` 顯示 capability；Docker build 有低成本
import/capability gate，不在啟動時 decode 測試圖。

參考固定版本的本地 package source，以及
[Pillow draft/thumbnail 語意](https://pillow.readthedocs.io/en/stable/reference/Image.html)、
[pillow-heif options](https://pillow-heif.readthedocs.io/en/latest/options.html)。
