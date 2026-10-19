# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
ARG TARGETARCH
COPY requirements-locks/ ./requirements-locks/
RUN cp "requirements-locks/linux-${TARGETARCH}-py312.txt" requirements.txt \
    && python -m pip wheel --require-hashes --only-binary=:all: --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INKTIME_ENVIRONMENT=production \
    INKTIME_DATA_DIR=/data \
    INKTIME_DATABASE=/data/inktime.db \
    INKTIME_RELEASE_DIR=/data/releases \
    INKTIME_BACKUP_DIR=/data/backups \
    INKTIME_CACHE_DIR=/data/cache \
    INKTIME_PHOTO_DIR=/photos \
    INKTIME_HOST=0.0.0.0 \
    INKTIME_PORT=8765

# Trixie has no fixed packages for the remaining util-linux, ACL, ncurses
# and Archive::Tar findings. Use signed, dated Debian packages for those
# families and their required libc ABI. Keep every other Forky package at
# low priority, retain required libraries, and verify versions during build.
# Runtime/upgrade acceptance remains covered by hosted image and NAS tests.
ARG DEBIAN_SNAPSHOT=20261012T000000Z
ARG DEBIAN_RUNTIME_SNAPSHOT=20261012T000000Z
ARG DEBIAN_SECURITY_SNAPSHOT=20261012T000000Z
RUN set -eux; \
    rm -f /etc/apt/sources.list.d/debian.sources; \
    printf '%s\n' \
        "deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/${DEBIAN_RUNTIME_SNAPSHOT}/ trixie main" \
        "deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/${DEBIAN_RUNTIME_SNAPSHOT}/ trixie-updates main" \
        "deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/${DEBIAN_SECURITY_SNAPSHOT}/ trixie-security main" \
        > /etc/apt/sources.list; \
    printf '%s\n' "deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/ forky main" \
        > /etc/apt/sources.list.d/inktime-runtime-fixes.list; \
    printf '%s\n' \
        'Package: *' \
        'Pin: release n=forky' \
        'Pin-Priority: 100' \
        '' \
        'Package: libsqlite3-0 bsdutils libblkid1 liblastlog2-2 libmount1 libsmartcols1 libuuid1 login mount util-linux libacl1 ncurses-base ncurses-bin libncurses6 libncursesw6 libtinfo6 perl-base libc6 libc-bin libc-gconv-modules-extra libsystemd0 libudev1' \
        'Pin: release n=forky' \
        'Pin-Priority: 1001' \
        > /etc/apt/preferences.d/inktime-runtime-fixes; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        bsdutils \
        gzip \
        libacl1 \
        libblkid1 \
        libc6 \
        libc-bin \
        liblastlog2-2 \
        libmount1 \
        libncursesw6 \
        libpcre2-8-0 \
        libssl3t64 \
        libsqlite3-0 \
        libsmartcols1 \
        libsystemd0 \
        libtinfo6 \
        libudev1 \
        libuuid1 \
        login \
        mount \
        ncurses-base \
        ncurses-bin \
        openssl \
        openssl-provider-legacy \
        perl-base \
        util-linux; \
    gzip_version="$(dpkg-query -W -f='${Version}' gzip)"; \
    dpkg --compare-versions "${gzip_version}" ge '1.13-1+deb13u1'; \
    perl_version="$(dpkg-query -W -f='${Version}' perl-base)"; \
    dpkg --compare-versions "${perl_version}" ge '5.42.3-1'; \
    for util_package in libblkid1 liblastlog2-2 libmount1 libsmartcols1 libuuid1 mount util-linux; do \
        util_version="$(dpkg-query -W -f='${Version}' "${util_package}")"; \
        dpkg --compare-versions "${util_version}" ge '2.42.4-1'; \
    done; \
    bsdutils_version="$(dpkg-query -W -f='${Version}' bsdutils)"; \
    dpkg --compare-versions "${bsdutils_version}" ge '1:2.42.4-1'; \
    login_version="$(dpkg-query -W -f='${Version}' login)"; \
    dpkg --compare-versions "${login_version}" ge '1:4.16.0-2+really2.42.4-1'; \
    acl_version="$(dpkg-query -W -f='${Version}' libacl1)"; \
    dpkg --compare-versions "${acl_version}" ge '2.4.0-1'; \
    for ncurses_package in ncurses-base ncurses-bin libtinfo6 libncursesw6; do \
        ncurses_version="$(dpkg-query -W -f='${Version}' "${ncurses_package}")"; \
        dpkg --compare-versions "${ncurses_version}" ge '6.6+20260608-2'; \
    done; \
    libc_version="$(dpkg-query -W -f='${Version}' libc6)"; \
    dpkg --compare-versions "${libc_version}" ge '2.43-7'; \
    for systemd_package in libsystemd0 libudev1; do \
        systemd_version="$(dpkg-query -W -f='${Version}' "${systemd_package}")"; \
        dpkg --compare-versions "${systemd_version}" ge '262-1'; \
    done; \
    pcre2_version="$(dpkg-query -W -f='${Version}' libpcre2-8-0)"; \
    dpkg --compare-versions "${pcre2_version}" ge '10.46-1~deb13u3'; \
    for openssl_package in libssl3t64 openssl openssl-provider-legacy; do \
        openssl_version="$(dpkg-query -W -f='${Version}' "${openssl_package}")"; \
        dpkg --compare-versions "${openssl_version}" ge '3.5.7-1~deb13u3'; \
    done; \
    sqlite_version="$(dpkg-query -W -f='${Version}' libsqlite3-0)"; \
    dpkg --compare-versions "${sqlite_version}" ge '3.53.2-1'; \
    apt-get check; \
    mount --version; \
    nsenter --version; \
    infocmp -V; \
    perl -e 'print "$^V\n"'; \
    python -c "import ctypes, hashlib, sqlite3, ssl, zlib; [ctypes.CDLL(name) for name in ('libacl.so.1', 'libtinfo.so.6')]; assert sqlite3.connect(':memory:').execute('SELECT 1').fetchone() == (1,); assert hashlib.sha256(b'ABI').hexdigest(); assert zlib.decompress(zlib.compress(b'ABI')) == b'ABI'; assert ssl.create_default_context().cert_store_stats()['x509_ca'] > 0"; \
    rm -f /etc/apt/sources.list.d/inktime-runtime-fixes.list \
        /etc/apt/preferences.d/inktime-runtime-fixes; \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 inktime \
    && useradd --uid 10001 --gid inktime --home-dir /app --shell /usr/sbin/nologin inktime

WORKDIR /app
COPY --from=builder /build/requirements.txt ./
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    python -m pip install --require-hashes --no-cache-dir --no-compile --no-index --find-links=/wheels -r requirements.txt \
    && python -m pip uninstall --yes pip setuptools \
    && rm -f requirements.txt
COPY --chown=inktime:inktime inktime/ ./inktime/
COPY --chown=inktime:inktime third_party/epdoptimize/ ./third_party/epdoptimize/
RUN python -c "from inktime.app.domain.photos.formats import ensure_image_codecs_registered; assert ensure_image_codecs_registered(), 'HEIF decoder unavailable'"
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
