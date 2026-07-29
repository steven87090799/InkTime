from __future__ import annotations

from pathlib import Path
import re

from tests.conftest import create_admin, login


NONCE = re.compile(r"script-src 'self' 'nonce-([^']+)'")
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)([^>]*)>", re.IGNORECASE)
EVENT_HANDLER = re.compile(
    r"\son(?:click|change|submit|load|error|input|keyup|keydown|mouseover|focus|blur)\s*=",
    re.IGNORECASE,
)


def test_csp_uses_unique_per_response_nonce_and_no_unsafe_inline_script(client, app):
    create_admin(app)
    login(client)
    first = client.get("/dashboard")
    second = client.get("/dashboard")

    first_policy = first.headers["Content-Security-Policy"]
    second_policy = second.headers["Content-Security-Policy"]
    first_nonce = NONCE.search(first_policy)
    second_nonce = NONCE.search(second_policy)
    assert first_nonce is not None
    assert second_nonce is not None
    assert first_nonce.group(1) != second_nonce.group(1)
    assert "script-src 'self' 'unsafe-inline'" not in first_policy
    assert "base-uri 'self'" in first_policy
    assert "object-src 'none'" in first_policy
    assert "frame-ancestors 'none'" in first_policy

    html = first.get_data(as_text=True)
    for attributes in INLINE_SCRIPT.findall(html):
        assert f'nonce="{first_nonce.group(1)}"' in attributes


def test_all_modern_templates_have_no_inline_event_attributes_or_untrusted_scripts():
    templates = Path(__file__).resolve().parents[2] / "inktime/app/web/templates"
    for path in templates.glob("*.html"):
        html = path.read_text(encoding="utf-8")
        assert EVENT_HANDLER.search(html) is None, path.name
        for attributes in INLINE_SCRIPT.findall(html):
            assert "nonce=" in attributes, path.name
