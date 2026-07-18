"""Intel N100／單機 Docker 的低資源 Gunicorn 預設。"""

import os


bind = "0.0.0.0:8765"
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
