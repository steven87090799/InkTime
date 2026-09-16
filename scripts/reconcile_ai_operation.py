#!/usr/bin/env python3
"""Inspect unresolved paid operations or record an explicit resend approval."""
import argparse
import json
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from inktime.app.db import Database
from inktime.app.repositories.billable_operations import BillableOperationRepository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--approve-resend", metavar="OPERATION_ID")
    parser.add_argument("--reason")
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("database does not exist")
    database = Database(args.database)
    if args.approve_resend:
        if not args.reason or not args.reason.strip():
            parser.error("--reason is required for explicit resend approval")
        BillableOperationRepository(database).approve_resend(args.approve_resend, reason=args.reason)
    with database.session() as connection:
        rows = connection.execute(
            "SELECT id,content_sha256,request_fingerprint,state,created_at FROM billable_operations "
            "WHERE state='started' ORDER BY created_at LIMIT 100"
        ).fetchall()
    print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
