#!/usr/bin/env python3
"""Print the schema version that this source tree can create."""

from inktime.app.db.migrations import MIGRATIONS


if __name__ == "__main__":
    print(max(migration.version for migration in MIGRATIONS))
