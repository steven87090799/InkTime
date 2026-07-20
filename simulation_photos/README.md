# 虛擬墨水屏照片投放區

將要測試的照片直接放在這個資料夾（可含子資料夾），支援 JPG、JPEG、PNG、WebP、HEIC、HEIF、TIFF 與 BMP。

預設 Docker 設定會把此資料夾唯讀掛載到容器內的 `/photos`。放入照片後：

1. 開啟 InkTime「維護」。
2. 按「掃描並送到虛擬墨水屏」。
3. 另開 `/virtual-display`；背景工作完成後會自動收到正式 Manifest 與 BIN Payload。

照片檔預設不會加入 Git，只有這份說明會保留在專案中。
