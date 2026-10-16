from __future__ import annotations

from pathlib import Path

from flask import Flask, request
from werkzeug.middleware.proxy_fix import ProxyFix

from inktime.app.bootstrap import bootstrap_services
from inktime.app.core.runtime_config import RuntimeConfig, resolve_runtime_config
from inktime.app.platform import configure_web_application


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

    @app.before_request
    def font_upload_body_limit():
        # Run before auth/CSRF can parse multipart form data.
        if request.endpoint == "rendering.upload_font":
            from inktime.app.api.rendering import MAX_FONT_BYTES
            request.max_content_length = MAX_FONT_BYTES + 1024 * 1024

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
    return app
