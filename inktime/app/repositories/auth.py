from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from uuid import uuid4

from inktime.app.core.security import hash_password, verify_password
from inktime.app.db import Database
from inktime.app.domain.auth import (
    AuthValidationError,
    SetupAlreadyCompleted,
    normalize_username,
    validate_role,
    validate_username,
)


class AuthRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def count_users(self) -> int:
        with self.database.session() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    @staticmethod
    def _insert_user(
        connection: sqlite3.Connection,
        *,
        username: str,
        normalized_username: str,
        password_hash: str,
        role: str,
        now: str,
    ) -> str:
        user_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO users(
                id,username,normalized_username,password_hash,role,
                session_version,password_changed_at,created_at
            )
            VALUES (?,?,?,?,?,1,?,?)
            """,
            (user_id, username, normalized_username, password_hash, role, now, now),
        )
        return user_id

    def create_user(self, username: object, password: object, role: object = "administrator") -> str:
        display, normalized = validate_username(username)
        resolved_role = validate_role(role)
        password_hash = hash_password(password)  # type: ignore[arg-type]
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self.database.transaction(operation="auth.create_user") as connection:
                return self._insert_user(
                    connection,
                    username=display,
                    normalized_username=normalized,
                    password_hash=password_hash,
                    role=resolved_role,
                    now=now,
                )
        except sqlite3.IntegrityError as exc:
            raise AuthValidationError(
                "這個帳號已經存在。",
                code="username_taken",
                http_status=409,
            ) from exc

    def create_initial_administrator(self, username: object, password: object) -> str:
        display, normalized = validate_username(username)
        password_hash = hash_password(password)  # type: ignore[arg-type]
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self.database.transaction(
                immediate=True,
                operation="auth.create_initial_administrator",
            ) as connection:
                if int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]) != 0:
                    raise SetupAlreadyCompleted()
                return self._insert_user(
                    connection,
                    username=display,
                    normalized_username=normalized,
                    password_hash=password_hash,
                    role="administrator",
                    now=now,
                )
        except sqlite3.IntegrityError as exc:
            raise AuthValidationError(
                "這個帳號已經存在。",
                code="username_taken",
                http_status=409,
            ) from exc

    def find_by_id(self, user_id: str):
        with self.database.session() as connection:
            return connection.execute(
                """
                SELECT id,username,role,enabled,password_hash,session_version
                FROM users WHERE id=?
                """,
                (user_id,),
            ).fetchone()

    def find_session_user(self, user_id: str, session_version: object):
        if type(session_version) is not int:
            return None
        with self.database.session() as connection:
            return connection.execute(
                """
                SELECT id,username,role,enabled,password_hash,session_version
                FROM users
                WHERE id=? AND enabled=1 AND session_version=?
                """,
                (user_id, session_version),
            ).fetchone()

    def authenticate(self, username: object, password: object):
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        normalized = normalize_username(username)
        with self.database.session() as connection:
            row = connection.execute(
                """
                SELECT * FROM users
                WHERE normalized_username=?
                   OR username=? COLLATE NOCASE
                """,
                (normalized, username.strip()),
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

    def change_password(self, user_id: str, current: object, new_password: object) -> None:
        if not isinstance(current, str):
            raise AuthValidationError("目前密碼不正確。", code="current_password_invalid")
        password_hash = hash_password(new_password)  # type: ignore[arg-type]
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction(operation="auth.change_password") as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE id=? AND enabled=1",
                (user_id,),
            ).fetchone()
            if row is None or not verify_password(row["password_hash"], current):
                raise AuthValidationError(
                    "目前密碼不正確。",
                    code="current_password_invalid",
                )
            connection.execute(
                """
                UPDATE users
                SET password_hash=?,password_changed_at=?,session_version=session_version+1
                WHERE id=?
                """,
                (password_hash, now, user_id),
            )

    def set_enabled(self, user_id: str, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise AuthValidationError("enabled 必須是 Boolean。", code="enabled_invalid_type")
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction(operation="auth.set_enabled") as connection:
            cursor = connection.execute(
                """
                UPDATE users
                SET enabled=?,disabled_at=?,session_version=session_version+1
                WHERE id=?
                """,
                (int(enabled), None if enabled else now, user_id),
            )
            if cursor.rowcount != 1:
                raise AuthValidationError("找不到使用者。", code="user_not_found", http_status=404)

    def set_role(self, user_id: str, role: object) -> None:
        resolved_role = validate_role(role)
        with self.database.transaction(operation="auth.set_role") as connection:
            cursor = connection.execute(
                "UPDATE users SET role=?,session_version=session_version+1 WHERE id=?",
                (resolved_role, user_id),
            )
            if cursor.rowcount != 1:
                raise AuthValidationError("找不到使用者。", code="user_not_found", http_status=404)

    def reset_password(self, user_id: str, new_password: object) -> None:
        password_hash = hash_password(new_password)  # type: ignore[arg-type]
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction(operation="auth.reset_password") as connection:
            cursor = connection.execute(
                """
                UPDATE users
                SET password_hash=?,password_changed_at=?,session_version=session_version+1
                WHERE id=?
                """,
                (password_hash, now, user_id),
            )
            if cursor.rowcount != 1:
                raise AuthValidationError("找不到使用者。", code="user_not_found", http_status=404)
