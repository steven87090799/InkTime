FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INKTIME_DATA_DIR=/data \
    INKTIME_DATABASE=/data/inktime.db \
    INKTIME_RELEASE_DIR=/data/releases \
    INKTIME_LEGACY_OUTPUT_DIR=/data/output \
    INKTIME_PHOTO_DIR=/photos

RUN groupadd --gid 10001 inktime \
    && useradd --uid 10001 --gid inktime --home-dir /app --shell /usr/sbin/nologin inktime

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=inktime:inktime . .
RUN mkdir -p /data /photos && chown -R inktime:inktime /data /app

ARG INKTIME_GIT_REVISION=unknown
ARG INKTIME_BUILD_TIME=unknown
ENV INKTIME_GIT_REVISION=${INKTIME_GIT_REVISION} \
    INKTIME_BUILD_TIME=${INKTIME_BUILD_TIME}

USER inktime
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health/ready', timeout=3).read()"]

CMD ["gunicorn", "--config", "gunicorn.conf.py", "server:app"]
