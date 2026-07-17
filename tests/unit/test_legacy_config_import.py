from __future__ import annotations

from inktime.app.db import Database, migrate
from scripts.import_legacy_config import import_config, load_legacy_config


def test_legacy_config_import_encrypts_secret_and_maps_settings(tmp_path):
    config_path = tmp_path / "config.py"
    config_path.write_text(
        'TIMEZONE="Asia/Taipei"\nMEMORY_THRESHOLD=72\nAPI_CHANNELS=[{"api_url":"https://example.test/v1","api_key":"super-secret","model_name":"vision-low"}]\n',
        encoding="utf-8",
    )
    database = Database(tmp_path / "inktime.db")
    migrate(database)
    result = import_config(load_legacy_config(config_path), database, master_secret="test-master")
    assert result["providers"] == 1
    with database.session() as connection:
        timezone = connection.execute(
            "SELECT value_json FROM settings WHERE key='general.timezone'"
        ).fetchone()[0]
        provider = connection.execute("SELECT api_key_secret FROM providers").fetchone()[0]
        raw_secret = connection.execute("SELECT encrypted_value FROM secrets").fetchone()[0]
    assert timezone == '"Asia/Taipei"'
    assert provider.startswith("provider.")
    assert b"super-secret" not in bytes(raw_secret)
