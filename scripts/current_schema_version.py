#!/usr/bin/env python3
"""Print the schema version that this source tree can create."""

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from inktime.app.db.migrations import MIGRATIONS


if __name__ == "__main__":
    print(max(migration.version for migration in MIGRATIONS))
