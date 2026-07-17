from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from inktime.app.core.security import hash_device_token, issue_device_token
from inktime.app.db import Database


class DeviceRepository:
    def __init__(self, database: Database, pepper: str) -> None:
        self.database = database
        self.pepper = pepper

    def list(self):
        with self.database.session() as connection:
            return connection.execute(
                """
                SELECT id, name, enabled, firmware_version, timezone, schedule, rotation,
                       last_seen_at, last_ip, last_download_at, last_release_id,
                       download_success_count, download_failure_count, wifi_rssi, battery_percent
                FROM devices ORDER BY name
                """
            ).fetchall()

    def create(self, name: str) -> tuple[str, str]:
        device_id = str(uuid4())
        token = issue_device_token()
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute(
                """
                INSERT INTO devices(id, name, token_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (device_id, name.strip(), hash_device_token(token, self.pepper), now, now),
            )
        return device_id, token

    def regenerate(self, device_id: str) -> str:
        token = issue_device_token()
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            cursor = connection.execute(
                "UPDATE devices SET token_hash=?, updated_at=? WHERE id=?",
                (hash_device_token(token, self.pepper), now, device_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(device_id)
        return token

    def authenticate(self, token: str, ip_address: str):
        digest = hash_device_token(token, self.pepper)
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE token_hash=? AND enabled=1", (digest,)
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE devices SET last_seen_at=?, last_ip=?, updated_at=? WHERE id=?",
                    (now, ip_address[:64], now, row["id"]),
                )
        return row

    def record_download(self, device_id: str, release_id: str, succeeded: bool) -> None:
        column = "download_success_count" if succeeded else "download_failure_count"
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute(
                f"""
                UPDATE devices SET {column}={column}+1, last_download_at=?,
                    last_release_id=CASE WHEN ? THEN ? ELSE last_release_id END, updated_at=?
                WHERE id=?
                """,
                (now, int(succeeded), release_id, now, device_id),
            )
