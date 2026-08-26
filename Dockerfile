# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY requirements.txt ./
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INKTIME_ENVIRONMENT=production \
    INKTIME_DATA_DIR=/data \
    INKTIME_DATABASE=/data/inktime.db \
    INKTIME_RELEASE_DIR=/data/releases \
    INKTIME_BACKUP_DIR=/data/backups \
    INKTIME_CACHE_DIR=/data/cache \
    INKTIME_LEGACY_OUTPUT_DIR=/data/output \
    INKTIME_ENABLE_LEGACY_WEBUI=false \
    INKTIME_PHOTO_DIR=/photos \
    INKTIME_HOST=0.0.0.0 \
    INKTIME_PORT=8765

# The pinned Python image may lag Debian security rebuilds. Debian trixie
# currently postpones the SQLite FTS5 fixes, so take only libsqlite3-0 from
# Debian forky, where the fixed 3.53.x package is available. Keep the source
# and pin temporary and verify the installed version before removing both.
RUN set -eux; \
    printf '%s\n' 'deb https://deb.debian.org/debian forky main' \
        > /etc/apt/sources.list.d/inktime-sqlite-fix.list; \
    printf '%s\n' \
        'Package: *' \
        'Pin: release n=forky' \
        'Pin-Priority: 100' \
        '' \
        'Package: libsqlite3-0' \
        'Pin: release n=forky' \
        'Pin-Priority: 1001' \
        > /etc/apt/preferences.d/inktime-sqlite-fix; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends --only-upgrade -y \
        bsdutils \
        libblkid1 \
        liblastlog2-2 \
        libmount1 \
        libsqlite3-0 \
        libsmartcols1 \
        libuuid1 \
        login \
        mount \
        util-linux; \
    sqlite_version="$(dpkg-query -W -f='${Version}' libsqlite3-0)"; \
    dpkg --compare-versions "${sqlite_version}" ge '3.53.2-1'; \
    rm -f /etc/apt/sources.list.d/inktime-sqlite-fix.list \
        /etc/apt/preferences.d/inktime-sqlite-fix; \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 inktime \
    && useradd --uid 10001 --gid inktime --home-dir /app --shell /usr/sbin/nologin inktime

WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-compile --no-index --find-links=/wheels -r requirements.txt \
    && python -m pip uninstall --yes pip setuptools \
    && rm -f requirements.txt
COPY --chown=inktime:inktime inktime/ ./inktime/
COPY --chown=inktime:inktime data/world_cities_zh.csv ./data/world_cities_zh.csv
COPY --chown=inktime:inktime scripts/container_health.py scripts/create_update_recovery.py scripts/migrate.py scripts/restore_backup.py ./scripts/
COPY --chown=inktime:inktime nas-deployment-contract.version ./nas-deployment-contract.version
COPY --chown=inktime:inktime server.py gunicorn.conf.py ./
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
