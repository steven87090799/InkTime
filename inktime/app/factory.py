from __future__ import annotations

from importlib import import_module
import logging
from pathlib import Path

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from inktime.app.bootstrap import bootstrap_services
from inktime.app.core.runtime_config import RuntimeConfig, resolve_runtime_config
from inktime.app.platform import configure_web_application


LOGGER = logging.getLogger(__name__)


def create_app(runtime_config: RuntimeConfig | None = None) -> Flask:
    """Create one isolated modern InkTime Web application."""

    config = resolve_runtime_config(runtime_config)
    web_root = Path(__file__).resolve().parent / "web"
    app = Flask(
        "inktime",
        template_folder=str(web_root / "templates"),
        static_folder=str(web_root / "static"),
        static_url_path="/static",
    )
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
    if config.proxy_trust:
        app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            app.wsgi_app,
            x_for=config.proxy_trust,
            x_proto=config.proxy_trust,
            x_host=config.proxy_trust,
            x_port=config.proxy_trust,
        )
    container = bootstrap_services(config, role="web")
    configure_web_application(app, config, container)

    if config.legacy_enabled:
        try:
            legacy_module = import_module("inktime.app.legacy.blueprint")
        except Exception as exc:
            app.extensions["inktime_legacy_available"] = False
            app.extensions["inktime_legacy_error"] = type(exc).__name__
            LOGGER.warning("Legacy 功能載入失敗；Modern App 繼續運作：%s", type(exc).__name__)
        else:
            try:
                legacy_module.register_legacy(app)
            except Exception:
                container.close()
                raise
            app.extensions["inktime_legacy_available"] = True
    else:
        app.extensions["inktime_legacy_available"] = False
    return app
