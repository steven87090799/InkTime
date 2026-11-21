"""Shared readiness checks for the Web process and container probes."""

from datetime import datetime, timedelta, timezone
import json
import sqlite3


def component_ready(connection: sqlite3.Connection, component: str) -> bool:
    row = connection.execute(
        "SELECT updated_at FROM observability_state WHERE key=?", (f"heartbeat:{component}",),
    ).fetchone()
    if row is None:
        return False
    try:
        heartbeat = datetime.fromisoformat(str(row[0]))
        if heartbeat.tzinfo is None:
            return False
        age = datetime.now(timezone.utc) - heartbeat
    except ValueError:
        return False
    max_seconds = 180
    if component == "scheduler":
        poll = connection.execute("SELECT value_json FROM settings WHERE key='scheduler.poll_seconds'").fetchone()
        try:
            interval = max(30, min(3600, int(json.loads(poll[0])))) if poll else 60
        except (ValueError, TypeError):
            interval = 60
        # The scheduler writes its heartbeat at most every five minutes.
        max_seconds = 330 + interval * 2
    return timedelta(seconds=-60) <= age <= timedelta(seconds=max_seconds)
