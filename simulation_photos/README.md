# 虛擬墨水屏照片投放區

將要測試的照片直接放在這個資料夾（可含子資料夾），支援 JPG、JPEG、PNG、WebP、HEIC、HEIF、TIFF 與 BMP。

開發 Compose 未覆寫 `INKTIME_PHOTO_PATH` 時，會把此資料夾唯讀掛載到容器內的 `/photos`；NAS 正式部署必須指定實際照片庫，不使用此預設。資料夾名稱不保證照片是合成資料，只放入你允許測試的內容。放入照片後：

1. 開啟 InkTime「維護」。
2. 按「掃描並送到虛擬墨水屏」。
3. 另開 `/virtual-display`；背景工作完成後會自動收到正式 Manifest 與 BIN Payload。

照片檔預設不會加入 Git，只有這份說明會保留在專案中。
