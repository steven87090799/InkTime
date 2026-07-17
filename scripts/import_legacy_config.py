#!/usr/bin/env python3
"""將舊 config.py 的可遷移欄位匯入新版設定與 Provider 資料表。"""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from inktime.app.db import Database, migrate
from inktime.app.repositories.providers import ProviderRepository
from inktime.app.repositories.settings import SecretStore, SettingsRepository


SETTING_MAP = {
    "TIMEZONE": "general.timezone",
    "MEMORY_THRESHOLD": "render.memory_threshold",
    "DAILY_PHOTO_QUANTITY": "render.quantity",
    "FONT_PATH": "render.font_path",
    "ENABLE_LEGACY_DEVICE_API": "device.legacy_api_enabled",
}


def load_legacy_config(path: Path):
    spec = importlib.util.spec_from_file_location("inktime_legacy_config_import", path)
    if spec is None or spec.loader is None:
        raise ValueError("無法載入舊設定檔")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def import_config(
    config,
    database: Database,
    *,
    master_secret: str | None,
    dry_run: bool = False,
) -> dict:
    settings = SettingsRepository(database)
    settings.ensure_defaults()
    mapped = {
        destination: getattr(config, source)
        for source, destination in SETTING_MAP.items()
        if hasattr(config, source)
    }
    channels = list(getattr(config, "API_CHANNELS", []) or [])
    if dry_run:
        return {"settings": sorted(mapped), "providers": len(channels)}

    for key, value in mapped.items():
        settings.update(key, value, changed_by="legacy-import", source_ip="local-script")

    if channels:
        if not master_secret:
            raise ValueError("匯入 Provider 前請設定 INKTIME_SECRET_KEY，或先啟動一次以建立 data/session.key")
        providers = ProviderRepository(database, SecretStore(database, master_secret))
        for index, channel in enumerate(channels, start=1):
            providers.save(
                {
                    "name": str(channel.get("name") or f"舊版 Provider {index}"),
                    "kind": "openai_compatible",
                    "base_url": str(channel.get("api_url") or ""),
                    "api_key": str(channel.get("api_key") or ""),
                    "priority": index,
                    "enabled": True,
                    "supports_batch": False,
                    "supports_json_schema": False,
                    "max_concurrency": 2,
                    "timeout_seconds": int(getattr(config, "TIMEOUT", 120) or 120),
                    "cooldown_seconds": int(getattr(config, "CHANNEL_FAILOVER_COOLDOWN_SEC", 300) or 300),
                },
                "legacy-import",
            )
        first_model = str(channels[0].get("model_name") or "").strip()
        if first_model:
            settings.update(
                "model.low_model", first_model, changed_by="legacy-import", source_ip="local-script"
            )
    return {"settings": sorted(mapped), "providers": len(channels)}


def main() -> None:
    parser = argparse.ArgumentParser(description="匯入舊版 InkTime config.py")
    parser.add_argument("config", type=Path, help="舊 config.py 路徑")
    parser.add_argument("--database", type=Path, default=Path("data/inktime.db"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--dry-run", action="store_true", help="只列出欄位，不寫入資料庫")
    args = parser.parse_args()
    config = load_legacy_config(args.config.resolve())
    database = Database(args.database)
    migrate(database, args.data_dir / "backups")
    secret_path = args.data_dir / "session.key"
    master_secret = os.environ.get("INKTIME_SECRET_KEY")
    if not master_secret and secret_path.is_file():
        master_secret = secret_path.read_text(encoding="utf-8").strip()
    result = import_config(config, database, master_secret=master_secret, dry_run=args.dry_run)
    print("將匯入設定：" + (", ".join(result["settings"]) or "無"))
    print(f"將匯入 Provider：{result['providers']} 個")
    print("DOWNLOAD_KEY 不會匯入；新版裝置必須建立獨立 Bearer Token。")


if __name__ == "__main__":
    main()
