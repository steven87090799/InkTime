#!/usr/bin/env python3
"""Create, verify, or restore an encrypted Secret Recovery Bundle."""

from __future__ import annotations
import argparse
import getpass
import json
import os
from pathlib import Path
from inktime.app.db import Database
from inktime.app.services.secret_recovery import RecoveryBundleService

parser = argparse.ArgumentParser()
parser.add_argument("action", choices=("create", "verify", "restore"))
parser.add_argument("bundle", type=Path)
parser.add_argument(
    "--database", type=Path, default=Path(os.environ.get("INKTIME_DATABASE", "/data/inktime.db"))
)
parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("INKTIME_DATA_DIR", "/data")))
args = parser.parse_args()
phrase = getpass.getpass("Recovery passphrase: ")
service = RecoveryBundleService(Database(args.database.resolve()), args.data_dir.resolve())
print(json.dumps(getattr(service, args.action)(args.bundle.resolve(), phrase), ensure_ascii=False))
