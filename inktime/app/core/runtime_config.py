from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_ENVIRONMENTS = {"development", "test", "production"}
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class RuntimeConfigurationError(ValueError):
    """Deployment configuration is invalid and startup must stop."""


def _boolean(value: object, *, name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise RuntimeConfigurationError(f"{name} 必須是 true 或 false")


def _integer(value: object, *, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigurationError(f"{name} 必須是整數") from exc
    if not minimum <= parsed <= maximum:
        raise RuntimeConfigurationError(f"{name} 必須介於 {minimum} 與 {maximum}")
    return parsed


def _path(value: object, *, base_dir: Path) -> Path:
    candidate = Path(str(value)).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


@dataclass(frozen=True)
class RuntimeConfig:
    """Immutable deployment/runtime settings shared by every process.

    Dynamic business settings stay in ``SettingsRepository`` and credentials
    stay in ``SecretStore``. This object deliberately contains neither.
    """

    environment: str
    data_dir: Path
    database_path: Path
    photo_dir: Path
    release_dir: Path
    backup_dir: Path
    cache_dir: Path
    host: str
    port: int
    timezone: str
    proxy_trust: int
    development: bool
    testing: bool
    worker_concurrency: int
    scheduler_identity: str
    cookie_secure: bool
    public_url: str = "http://127.0.0.1"
    allow_insecure_http: bool = False
    allow_unsafe_network_database: bool = False

    def __post_init__(self) -> None:
        environment = self.environment.strip().casefold()
        if environment not in _ENVIRONMENTS:
            raise RuntimeConfigurationError("INKTIME_ENVIRONMENT 必須是 development、test 或 production")
        if not self.host.strip():
            raise RuntimeConfigurationError("INKTIME_HOST 不可空白")
        if not 1 <= self.port <= 65535:
            raise RuntimeConfigurationError("INKTIME_PORT 必須介於 1 與 65535")
        if not 0 <= self.proxy_trust <= 10:
            raise RuntimeConfigurationError("INKTIME_PROXY_TRUST 必須介於 0 與 10")
        if not 1 <= self.worker_concurrency <= 32:
            raise RuntimeConfigurationError("INKTIME_WORKER_CONCURRENCY 必須介於 1 與 32")
        if not self.scheduler_identity.strip():
            raise RuntimeConfigurationError("INKTIME_SCHEDULER_IDENTITY 不可空白")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise RuntimeConfigurationError("INKTIME_TIMEZONE 不是有效的 IANA 時區") from exc
        for name in (
            "data_dir",
            "database_path",
            "photo_dir",
            "release_dir",
            "backup_dir",
            "cache_dir",
        ):
            if not getattr(self, name).is_absolute():
                raise RuntimeConfigurationError(f"{name} 必須解析為絕對路徑")
        if environment == "production":
            if self.testing or self.development:
                raise RuntimeConfigurationError("Production 不得啟用 testing 或 development")
            if self.data_dir == _PROJECT_ROOT / "data":
                raise RuntimeConfigurationError("Production 必須明確設定隔離的 INKTIME_DATA_DIR")

    @classmethod
    def from_sources(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        base_dir: Path | None = None,
        environment: str | None = None,
        data_dir: Path | str | None = None,
        database_path: Path | str | None = None,
        photo_dir: Path | str | None = None,
        release_dir: Path | str | None = None,
        backup_dir: Path | str | None = None,
        cache_dir: Path | str | None = None,
        host: str | None = None,
        port: int | str | None = None,
        timezone: str | None = None,
        proxy_trust: int | str | None = None,
        development: bool | str | None = None,
        testing: bool | str | None = None,
        worker_concurrency: int | str | None = None,
        scheduler_identity: str | None = None,
        cookie_secure: bool | str | None = None,
        public_url: str | None = None,
        allow_insecure_http: bool | str | None = None,
        allow_unsafe_network_database: bool | str | None = None,
    ) -> "RuntimeConfig":
        """Resolve explicit arguments, then environment, then safe defaults."""

        source = os.environ if environ is None else environ
        root = (base_dir or _PROJECT_ROOT).expanduser().resolve()
        resolved_environment = (
            str(environment if environment is not None else source.get("INKTIME_ENVIRONMENT", "development"))
            .strip()
            .casefold()
        )
        production = resolved_environment == "production"
        resolved_testing = _boolean(
            testing if testing is not None else source.get("INKTIME_TESTING", resolved_environment == "test"),
            name="INKTIME_TESTING",
        )
        resolved_development = _boolean(
            development
            if development is not None
            else source.get("INKTIME_DEVELOPMENT", resolved_environment == "development"),
            name="INKTIME_DEVELOPMENT",
        )

        default_data = Path("/data") if production else root / "data"
        resolved_data = _path(
            data_dir if data_dir is not None else source.get("INKTIME_DATA_DIR", default_data),
            base_dir=root,
        )
        resolved_database = _path(
            database_path
            if database_path is not None
            else source.get("INKTIME_DATABASE", resolved_data / "inktime.db"),
            base_dir=root,
        )

        def resolve_child(explicit: Path | str | None, env_name: str, default: Path) -> Path:
            return _path(
                explicit if explicit is not None else source.get(env_name, default),
                base_dir=root,
            )

        return cls(
            environment=resolved_environment,
            data_dir=resolved_data,
            database_path=resolved_database,
            photo_dir=resolve_child(
                photo_dir,
                "INKTIME_PHOTO_DIR",
                Path("/photos") if production else root / "simulation_photos",
            ),
            release_dir=resolve_child(release_dir, "INKTIME_RELEASE_DIR", resolved_data / "releases"),
            backup_dir=resolve_child(backup_dir, "INKTIME_BACKUP_DIR", resolved_data / "backups"),
            cache_dir=resolve_child(cache_dir, "INKTIME_CACHE_DIR", resolved_data / "cache"),
            host=str(host if host is not None else source.get("INKTIME_HOST", "127.0.0.1")),
            port=_integer(
                port if port is not None else source.get("INKTIME_PORT", 8765),
                name="INKTIME_PORT",
                minimum=1,
                maximum=65535,
            ),
            timezone=str(
                timezone
                if timezone is not None
                else source.get("INKTIME_TIMEZONE", source.get("TZ", "Asia/Taipei"))
            ),
            proxy_trust=_integer(
                proxy_trust if proxy_trust is not None else source.get("INKTIME_PROXY_TRUST", 0),
                name="INKTIME_PROXY_TRUST",
                minimum=0,
                maximum=10,
            ),
            development=resolved_development,
            testing=resolved_testing,
            worker_concurrency=_integer(
                worker_concurrency
                if worker_concurrency is not None
                else source.get("INKTIME_WORKER_CONCURRENCY", 2),
                name="INKTIME_WORKER_CONCURRENCY",
                minimum=1,
                maximum=32,
            ),
            scheduler_identity=str(
                scheduler_identity
                if scheduler_identity is not None
                else source.get("INKTIME_SCHEDULER_IDENTITY", "inktime-scheduler")
            ),
            cookie_secure=_boolean(
                cookie_secure
                if cookie_secure is not None
                else source.get("INKTIME_COOKIE_SECURE", production),
                name="INKTIME_COOKIE_SECURE",
            ),
            public_url=str(
                public_url
                if public_url is not None
                else source.get(
                    "INKTIME_PUBLIC_URL", "https://localhost" if production else "http://127.0.0.1"
                )
            ),
            allow_insecure_http=_boolean(
                allow_insecure_http
                if allow_insecure_http is not None
                else source.get("INKTIME_ALLOW_INSECURE_HTTP", not production),
                name="INKTIME_ALLOW_INSECURE_HTTP",
            ),
            allow_unsafe_network_database=_boolean(
                allow_unsafe_network_database
                if allow_unsafe_network_database is not None
                else source.get("INKTIME_ALLOW_UNSAFE_NETWORK_DATABASE", False),
                name="INKTIME_ALLOW_UNSAFE_NETWORK_DATABASE",
            ),
        )

    def diagnostic_summary(self) -> dict[str, object]:
        """Return a bounded, credential-free runtime summary for diagnostics."""

        return {
            "environment": self.environment,
            "data_dir": "<configured:absolute>",
            "database_path": "<configured:absolute>",
            "photo_dir": "<configured:absolute>",
            "release_dir": "<configured:absolute>",
            "backup_dir": "<configured:absolute>",
            "cache_dir": "<configured:absolute>",
            "host": "<configured>",
            "port": self.port,
            "timezone": self.timezone,
            "proxy_trust": self.proxy_trust,
            "development": self.development,
            "testing": self.testing,
            "worker_concurrency": self.worker_concurrency,
            "scheduler_identity": self.scheduler_identity,
            "cookie_secure": self.cookie_secure,
            "public_url_scheme": self.public_url.split(":", 1)[0],
            "allow_insecure_http": self.allow_insecure_http,
            "allow_unsafe_network_database": self.allow_unsafe_network_database,
        }


def resolve_runtime_config(runtime_config: RuntimeConfig | None = None) -> RuntimeConfig:
    """Use an explicit immutable config unchanged, otherwise resolve once."""

    return runtime_config if runtime_config is not None else RuntimeConfig.from_sources()
