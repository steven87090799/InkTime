"""Intel N100／單機 Docker 的低資源 Gunicorn 預設。"""

import os

from inktime.app.core.runtime_config import resolve_runtime_config


runtime_config = resolve_runtime_config()
bind = f"{runtime_config.host}:{runtime_config.port}"
workers = max(1, int(os.environ.get("INKTIME_WEB_WORKERS", "1")))
threads = max(1, int(os.environ.get("INKTIME_WEB_THREADS", "2")))
worker_class = "gthread"
timeout = 120
graceful_timeout = 30
keepalive = 5
errorlog = "-"
accesslog = "-" if os.environ.get("INKTIME_ACCESS_LOG", "0") == "1" else None
loglevel = os.environ.get("INKTIME_GUNICORN_LOG_LEVEL", "warning").lower()
capture_output = True
worker_tmp_dir = "/tmp"
