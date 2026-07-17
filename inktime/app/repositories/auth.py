from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from inktime.app.core.security import hash_password, verify_password
from inktime.app.db import Database


class AuthRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def count_users(self) -> int:
        with self.database.session() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def create_user(self, username: str, password: str, role: str = "administrator") -> str:
        if role not in {"administrator", "viewer"}:
            raise ValueError("不支援的角色")
        now = datetime.now(timezone.utc).isoformat()
        user_id = str(uuid4())
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO users(id, username, password_hash, role, password_changed_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, username.strip(), hash_password(password), role, now, now),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return user_id

    def find_by_id(self, user_id: str):
        with self.database.session() as connection:
            return connection.execute(
                "SELECT id, username, role, enabled, password_hash FROM users WHERE id=?",
                (user_id,),
            ).fetchone()

    def authenticate(self, username: str, password: str):
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username=? COLLATE NOCASE", (username.strip(),)
            ).fetchone()
        if row is None or not row["enabled"] or not verify_password(row["password_hash"], password):
            return None
        return row

    def ip_blocked(self, ip_address: str, *, maximum: int = 5, minutes: int = 15) -> bool:
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        with self.database.session() as connection:
            failures = connection.execute(
                """
                SELECT COUNT(*) FROM login_attempts
                WHERE ip_address=? AND succeeded=0 AND attempted_at>=?
                """,
                (ip_address, since),
            ).fetchone()[0]
        return int(failures) >= maximum

    def record_login(
        self, username: str, ip_address: str, succeeded: bool, user_id: str | None = None
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute(
                "INSERT INTO login_attempts(username, ip_address, succeeded, attempted_at) VALUES (?, ?, ?, ?)",
                (username[:128], ip_address[:64], int(succeeded), now),
            )
            if succeeded and user_id:
                connection.execute(
                    "DELETE FROM login_attempts WHERE username=? AND ip_address=? AND succeeded=0",
                    (username[:128], ip_address[:64]),
                )
                connection.execute(
                    "UPDATE users SET last_login_at=?, failed_attempts=0, locked_until=NULL WHERE id=?",
                    (now, user_id),
                )

    def change_password(self, user_id: str, current: str, new_password: str) -> None:
        row = self.find_by_id(user_id)
        if row is None or not verify_password(row["password_hash"], current):
            raise ValueError("目前密碼不正確")
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute(
                "UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?",
                (hash_password(new_password), now, user_id),
            )
