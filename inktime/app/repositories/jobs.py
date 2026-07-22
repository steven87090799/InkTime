from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Iterable, Iterator
from uuid import uuid4

from inktime.app.db import Database


ACTIVE_STATUSES = {"preparing", "running", "pausing", "retrying"}
TERMINAL_STATUSES = {"completed", "completed_with_errors", "failed", "cancelled"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def iter_photo_ids(
        self, *, statuses: tuple[str, ...] = ("preprocessed",), limit: int | None = None
    ) -> Iterator[str]:
        placeholders = ",".join("?" for _ in statuses)
        last_id = ""
        remaining = limit
        while remaining is None or remaining > 0:
            batch_size = min(500, remaining) if remaining is not None else 500
            with self.database.session() as connection:
                rows = connection.execute(
                    f"SELECT id FROM photos WHERE lifecycle_status='active' AND status IN ({placeholders}) AND id>? ORDER BY id LIMIT ?",  # noqa: S608 -- placeholders are generated, values remain bound
                    (*statuses, last_id, batch_size),
                ).fetchall()
            if not rows:
                break
            for row in rows:
                yield str(row["id"])
            last_id = str(rows[-1]["id"])
            if remaining is not None:
                remaining -= len(rows)

    def create(
        self,
        *,
        name: str,
        strategy: str,
        settings: dict,
        photo_ids: Iterable[str],
        created_by: str | None,
        budget_limit: float | None = None,
    ) -> str:
        job_id = str(uuid4())
        now = utc_now()
        total = 0
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO jobs(id, kind, name, status, strategy, settings_json,
                                     budget_limit, created_by, created_at)
                    VALUES (?, 'analysis', ?, 'pending', ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        name,
                        strategy,
                        json.dumps(settings, ensure_ascii=False),
                        budget_limit,
                        created_by,
                        now,
                    ),
                )
                batch: list[tuple] = []
                for photo_id in photo_ids:
                    batch.append((str(uuid4()), job_id, str(photo_id), now))
                    if len(batch) == 500:
                        connection.executemany(
                            "INSERT OR IGNORE INTO job_items(id, job_id, photo_id, available_at) VALUES (?, ?, ?, ?)",
                            batch,
                        )
                        total += len(batch)
                        batch.clear()
                if batch:
                    connection.executemany(
                        "INSERT OR IGNORE INTO job_items(id, job_id, photo_id, available_at) VALUES (?, ?, ?, ?)",
                        batch,
                    )
                    total += len(batch)
                # OR IGNORE 可能排除同一工作中的重複照片，以實際筆數為準。
                total = int(
                    connection.execute("SELECT COUNT(*) FROM job_items WHERE job_id=?", (job_id,)).fetchone()[
                        0
                    ]
                )
                connection.execute("UPDATE jobs SET total_items=? WHERE id=?", (total, job_id))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        self.add_event(job_id, "created", f"已建立工作，共 {total} 張照片")
        return job_id

    def get(self, job_id: str):
        with self.database.session() as connection:
            return connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def create_maintenance(self, *, kind: str, name: str, settings: dict, created_by: str | None) -> str:
        if kind not in {"scan", "backup", "render", "cleanup", "virtual_display"}:
            raise ValueError("不支援的維護工作")
        job_id = str(uuid4())
        item_id = str(uuid4())
        now = utc_now()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO jobs(id,kind,name,status,strategy,settings_json,total_items,created_by,created_at) VALUES (?,?,?,'pending','local',?,1,?,?)",
                    (job_id, kind, name, json.dumps(settings, ensure_ascii=False), created_by, now),
                )
                connection.execute(
                    "INSERT INTO job_items(id,job_id,photo_id,available_at) VALUES (?,?,NULL,?)",
                    (item_id, job_id, now),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        self.add_event(job_id, "created", f"已建立 {kind} 維護工作")
        return job_id

    def list(self, limit: int = 100):
        with self.database.session() as connection:
            return connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()

    def list_items(self, job_id: str, *, limit: int = 100, offset: int = 0):
        with self.database.session() as connection:
            return connection.execute(
                "SELECT * FROM job_items WHERE job_id=? ORDER BY id LIMIT ? OFFSET ?",
                (job_id, limit, offset),
            ).fetchall()

    def add_event(self, job_id: str, event: str, message: str, details: dict | None = None) -> None:
        with self.database.session() as connection:
            connection.execute(
                "INSERT INTO job_events(job_id,event,message,details_json,created_at) VALUES (?,?,?,?,?)",
                (job_id, event, message, json.dumps(details or {}, ensure_ascii=False), utc_now()),
            )

    def transition(self, job_id: str, from_statuses: set[str], to_status: str, event: str) -> bool:
        now = utc_now()
        placeholders = ",".join("?" for _ in from_statuses)
        with self.database.session() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET status=?, heartbeat_at=? WHERE id=? AND status IN ({placeholders})",  # noqa: S608 -- only placeholder count is dynamic
                (to_status, now, job_id, *sorted(from_statuses)),
            )
        if cursor.rowcount:
            self.add_event(job_id, event, f"工作狀態已變更為 {to_status}")
        return bool(cursor.rowcount)

    def request_pause(self, job_id: str) -> bool:
        now = utc_now()
        with self.database.session() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status='pausing', pause_requested_at=? WHERE id=? AND status IN ('running','retrying')",
                (now, job_id),
            )
        if cursor.rowcount:
            self.add_event(job_id, "pause_requested", "已要求暫停；目前處理中的項目完成後停止")
        return bool(cursor.rowcount)

    def acknowledge_pause(self, job_id: str) -> None:
        self.transition(job_id, {"pausing"}, "paused", "paused")

    def cancel(self, job_id: str) -> bool:
        now = utc_now()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE jobs SET status='cancelled', cancel_requested_at=?, completed_at=?
                    WHERE id=? AND status NOT IN ('completed','completed_with_errors','failed','cancelled')
                    """,
                    (now, now, job_id),
                )
                if cursor.rowcount:
                    connection.execute(
                        "UPDATE job_items SET status='cancelled', completed_at=? WHERE job_id=? AND status IN ('pending','retrying')",
                        (now, job_id),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if cursor.rowcount:
            self.add_event(job_id, "cancelled", "工作已取消，不會再送出新請求")
        return bool(cursor.rowcount)

    def claim(self, job_id: str, worker_id: str, limit: int, lease_seconds: int = 300):
        now = utc_now()
        lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                job = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                if job is None or job["status"] not in {"running", "retrying"}:
                    connection.execute("COMMIT")
                    return []
                rows = connection.execute(
                    """
                    SELECT * FROM job_items
                    WHERE job_id=? AND status='pending' AND available_at<=?
                    ORDER BY id LIMIT ?
                    """,
                    (job_id, now, limit),
                ).fetchall()
                ids = [row["id"] for row in rows]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    connection.execute(
                        f"""
                        UPDATE job_items SET status='running', worker_id=?, started_at=?,
                                             lease_until=?, attempts=attempts+1
                        WHERE id IN ({placeholders})
                        """,
                        (worker_id, now, lease_until, *ids),
                    )
                    rows = connection.execute(
                        f"SELECT * FROM job_items WHERE id IN ({placeholders})",  # noqa: S608 -- ids are bound parameters
                        ids,
                    ).fetchall()
                connection.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (now, job_id))
                connection.execute("COMMIT")
                return rows
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def renew_leases(self, job_id: str, worker_id: str, lease_seconds: int = 300) -> int:
        """延長目前 Worker 的租約，避免長時間掃描或模型呼叫被誤判為失聯。"""

        now = utc_now()
        lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self.database.session() as connection:
            cursor = connection.execute(
                """
                UPDATE job_items SET lease_until=?
                WHERE job_id=? AND worker_id=? AND status='running'
                """,
                (lease_until, job_id, worker_id),
            )
            connection.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (now, job_id))
        return int(cursor.rowcount)

    def complete_item(self, job_id: str, item_id: str, result: dict, actual_cost: float = 0) -> None:
        now = utc_now()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                status = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                if status is None or status["status"] == "cancelled":
                    connection.execute(
                        "UPDATE job_items SET status='cancelled', completed_at=?, lease_until=NULL WHERE id=?",
                        (now, item_id),
                    )
                else:
                    stage = str(result.get("stage") or "completed")[:64]
                    cursor = connection.execute(
                        """
                        UPDATE job_items SET status='completed', completed_at=?, result_json=?,
                                             lease_until=NULL, estimated_cost=?, stage=?
                        WHERE id=? AND status='running'
                        """,
                        (now, json.dumps(result, ensure_ascii=False), actual_cost, stage, item_id),
                    )
                    if cursor.rowcount:
                        connection.execute(
                            "UPDATE jobs SET completed_items=completed_items+1, spent=spent+?, heartbeat_at=? WHERE id=?",
                            (actual_cost, now, job_id),
                        )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def defer_item(self, item_id: str) -> None:
        """預算阻擋時歸還租約，不把尚未送出的項目記成分析失敗。"""
        with self.database.session() as connection:
            connection.execute(
                """
                UPDATE job_items
                SET status='pending',worker_id=NULL,lease_until=NULL,available_at=?,attempts=MAX(0,attempts-1)
                WHERE id=? AND status='running'
                """,
                (utc_now(), item_id),
            )

    def fail_item(
        self, job_id: str, item_id: str, error_code: str, message: str, *, max_attempts: int = 3
    ) -> None:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                item = connection.execute(
                    "SELECT attempts, photo_id FROM job_items WHERE id=?", (item_id,)
                ).fetchone()
                terminal = item is None or int(item["attempts"]) >= max_attempts
                if terminal:
                    connection.execute(
                        "UPDATE job_items SET status='failed', completed_at=?, error_code=?, lease_until=NULL WHERE id=?",
                        (now, error_code, item_id),
                    )
                    connection.execute("UPDATE jobs SET failed_items=failed_items+1 WHERE id=?", (job_id,))
                else:
                    delay = min(300, 2 ** int(item["attempts"]))
                    available = (now_dt + timedelta(seconds=delay)).isoformat()
                    connection.execute(
                        "UPDATE job_items SET status='pending', available_at=?, error_code=?, lease_until=NULL WHERE id=?",
                        (available, error_code, item_id),
                    )
                fingerprint = hashlib.sha256(f"{job_id}:{item_id}:{error_code}".encode()).hexdigest()
                existing_error = connection.execute(
                    "SELECT id FROM job_errors WHERE fingerprint=? AND resolved_at IS NULL",
                    (fingerprint,),
                ).fetchone()
                if existing_error:
                    connection.execute(
                        "UPDATE job_errors SET occurrences=occurrences+1,last_seen_at=?,message=? WHERE id=?",
                        (now, message[:1000], existing_error["id"]),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO job_errors(job_id,job_item_id,photo_id,component,error_code,fingerprint,severity,message,first_seen_at,last_seen_at)
                        VALUES (?,?,?,'worker',?,?,?,?,?,?)
                        """,
                        (
                            job_id,
                            item_id,
                            item["photo_id"] if item else None,
                            error_code,
                            fingerprint,
                            "error",
                            message[:1000],
                            now,
                            now,
                        ),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def finalize_if_done(self, job_id: str) -> bool:
        now = utc_now()
        with self.database.session() as connection:
            counts = connection.execute(
                """
                SELECT SUM(status IN ('pending','running','retrying')) AS active,
                       SUM(status='failed') AS failed
                FROM job_items WHERE job_id=?
                """,
                (job_id,),
            ).fetchone()
            if counts is None or int(counts["active"] or 0) > 0:
                return False
            target = "completed_with_errors" if int(counts["failed"] or 0) else "completed"
            cursor = connection.execute(
                "UPDATE jobs SET status=?, completed_at=?, heartbeat_at=? WHERE id=? AND status IN ('running','retrying')",
                (target, now, now, job_id),
            )
        if cursor.rowcount:
            self.add_event(job_id, "finished", f"工作已結束：{target}")
        return bool(cursor.rowcount)

    def recover_stale(self) -> int:
        now = utc_now()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE job_items SET status='pending', worker_id=NULL, lease_until=NULL, available_at=?
                    WHERE status='running' AND (lease_until IS NULL OR lease_until<?)
                    """,
                    (now, now),
                )
                connection.execute("UPDATE jobs SET status='paused' WHERE status='pausing'")
                connection.execute("COMMIT")
                return int(cursor.rowcount)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def retry_failed(self, job_id: str) -> int:
        now = utc_now()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE job_items SET status='pending', available_at=?, error_code=NULL, completed_at=NULL WHERE job_id=? AND status='failed'",
                    (now, job_id),
                )
                connection.execute(
                    "UPDATE jobs SET status='pending', failed_items=0, completed_at=NULL WHERE id=? AND status IN ('failed','completed_with_errors')",
                    (job_id,),
                )
                connection.execute("COMMIT")
                return int(cursor.rowcount)
            except Exception:
                connection.execute("ROLLBACK")
                raise
