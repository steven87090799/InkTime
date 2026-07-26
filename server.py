#!/usr/bin/env python3
"""InkTime 正式 Web 入口；Gunicorn 請使用 ``server:app``。"""

import mimetypes

from inktime.app.factory import create_app


app = create_app()


if __name__ == "__main__":
    runtime_config = app.extensions["inktime_runtime_config"]
    mimetypes.add_type("application/octet-stream", ".bin")
    print(
        f"[InkTime] 管理介面：http://{runtime_config.host}:{runtime_config.port}/"
    )
    print("[InkTime] 開發伺服器只適用於本機測試；正式環境請使用 Gunicorn。")
    app.run(host=runtime_config.host, port=runtime_config.port, debug=False)
