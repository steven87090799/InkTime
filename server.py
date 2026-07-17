#!/usr/bin/env python3
"""InkTime 正式 Web 入口；Gunicorn 請使用 ``server:app``。"""

from pathlib import Path
import mimetypes
import os

import legacy_server
from inktime.app.platform import initialize_platform


ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("INKTIME_DATA_DIR", ROOT_DIR / "data")).expanduser().resolve()
app = legacy_server.app
DATABASE_PATH = Path(os.environ.get("INKTIME_DATABASE", legacy_server.DB_PATH)).expanduser().resolve()
RELEASE_DIR = Path(os.environ.get("INKTIME_RELEASE_DIR", DATA_DIR / "releases")).expanduser().resolve()

initialize_platform(
    app,
    database_path=DATABASE_PATH,
    data_dir=DATA_DIR,
    release_dir=RELEASE_DIR,
)
# 舊 URL 金鑰 API 僅在管理員明確啟用並重啟後開放；預設保持關閉。
legacy_server.ENABLE_LEGACY_DEVICE_API = bool(
    app.extensions["inktime_settings_repository"].get("device.legacy_api_enabled", False)
)


if __name__ == "__main__":
    mimetypes.add_type("application/octet-stream", ".bin")
    host = os.environ.get("INKTIME_HOST", "127.0.0.1")
    port = int(os.environ.get("INKTIME_PORT", "8765"))
    print(f"[InkTime] 管理介面：http://{host}:{port}/")
    print("[InkTime] 開發伺服器只適用於本機測試；正式環境請使用 Gunicorn。")
    app.run(host=host, port=port, debug=False)
