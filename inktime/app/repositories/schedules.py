from __future__ import annotations

from builtins import list as builtin_list
from datetime import datetime, timedelta
import json
from typing import Any
from zoneinfo import ZoneInfo

from inktime.app.repositories.jobs import utc_now
from inktime.app.db import Database


TASK_DEFAULTS: dict[str, dict[str, Any]] = {
    "incremental_scan": {
        "name": "增量掃描", "kind": "scan", "cron": "0 2 * * *", "start_time": "02:00",
        "window_start": "00:00", "window_end": "06:00", "timeout_seconds": 14400,
        "retry_count": 2, "retry_interval_seconds": 900,
        "config": {"library_name": "主要照片庫", "root_path": "", "mode": "incremental", "build_thumbnails": True,
                   "batch_size": 500, "concurrency": 1, "catch_up": True, "delay_high_load": True},
    },
    "full_reconcile": {
        "name": "完整一致性掃描", "kind": "scan", "cron": "0 3 * * 0", "start_time": "03:00",
        "window_start": "00:00", "window_end": "08:00", "timeout_seconds": 28800,
        "retry_count": 1, "retry_interval_seconds": 1800,
        "config": {"library_name": "主要照片庫", "root_path": "", "mode": "full", "check_missing": True,
                   "check_moves": True, "verify_hashes": False, "clean_orphan_thumbnails": True,
                   "missing_safe_percent": 10, "delay_high_load": True},
    },
    "display_prepare": {
        "name": "換圖準備", "kind": "render", "cron": "30 7 * * *", "start_time": "07:30",
        "window_start": None, "window_end": None, "timeout_seconds": 3600,
        "retry_count": 1, "retry_interval_seconds": 600,
        "config": {"lead_minutes": 30, "display_times": ["08:00"], "daily_count": 1, "device_ids": [],
                   "candidate_years": [], "prefetch_count": 1, "ai_fallback": "use_existing", "render_fallback": "keep_current"},
    },
    "ai_schedule": {
        "name": "AI 排程入口", "kind": "analysis", "cron": "0 1 * * *", "start_time": "01:00",
        "window_start": "00:00", "window_end": "06:00", "timeout_seconds": 14400,
        "retry_count": 2, "retry_interval_seconds": 900,
        "config": {"mode": "disabled", "fixed_times": ["01:00"], "night_window": ["00:00", "06:00"],
                   "new_photo_delay_minutes": 30, "strategy": "smart_two_stage", "concurrency": 1},
    },
    "cache_cleanup": {
        "name": "快取清理", "kind": "cleanup", "cron": "30 4 * * *", "start_time": "04:30",
        "window_start": "00:00", "window_end": "06:00", "timeout_seconds": 3600,
        "retry_count": 1, "retry_interval_seconds": 900,
        "config": {"max_bytes": 5368709120, "retention_days": 30, "clean_orphans": True},
    },
}


class ScheduledTaskRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ensure_defaults(self, timezone: str = "Asia/Taipei") -> None:
        now = datetime.now(ZoneInfo(timezone))
        with self.database.session() as connection:
            for key, task in TASK_DEFAULTS.items():
                next_run = self._next_run(task["cron"], now, [])
                connection.execute(
                    """
                    INSERT OR IGNORE INTO scheduled_tasks(
                        key,name,kind,cron,start_time,window_start,window_end,timeout_seconds,retry_count,
                        retry_interval_seconds,next_run,config_json,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (key, task["name"], task["kind"], task["cron"], task["start_time"], task["window_start"],
                     task["window_end"], task["timeout_seconds"], task["retry_count"], task["retry_interval_seconds"],
                     next_run.isoformat(), json.dumps(task["config"], ensure_ascii=False), utc_now(), utc_now()),
                )

    def list(self) -> builtin_list[dict[str, Any]]:
        with self.database.session() as connection:
            rows = connection.execute("SELECT * FROM scheduled_tasks ORDER BY key").fetchall()
        return [self._row(row) for row in rows]

    def get(self, key: str) -> dict[str, Any] | None:
        with self.database.session() as connection:
            row = connection.execute("SELECT * FROM scheduled_tasks WHERE key=?", (key,)).fetchone()
        return self._row(row) if row else None

    def update(self, key: str, payload: dict[str, Any], timezone: str) -> dict[str, Any]:
        current = self.get(key)
        if current is None:
            raise KeyError(key)
        cron = self._cron(str(payload.get("cron", current["cron"])))
        weekdays = self._weekdays(payload.get("weekdays", current["weekdays"]))
        start_time = self._clock(str(payload.get("start_time", current["start_time"])))
        window_start = self._optional_clock(payload.get("window_start", current["window_start"]))
        window_end = self._optional_clock(payload.get("window_end", current["window_end"]))
        enabled = bool(payload.get("enabled", current["enabled"]))
        config = current["config"] | dict(payload.get("config") or {})
        now = datetime.now(ZoneInfo(timezone))
        next_run = self._next_run(cron, now, weekdays).isoformat() if enabled else None
        values = (
            int(enabled), cron, json.dumps(weekdays), start_time, window_start, window_end,
            self._bounded(payload.get("timeout_seconds", current["timeout_seconds"]), 30, 86400),
            self._bounded(payload.get("retry_count", current["retry_count"]), 0, 10),
            self._bounded(payload.get("retry_interval_seconds", current["retry_interval_seconds"]), 30, 86400),
            next_run, json.dumps(config, ensure_ascii=False), utc_now(), key,
        )
        with self.database.session() as connection:
            connection.execute(
                """UPDATE scheduled_tasks SET enabled=?,cron=?,weekdays_json=?,start_time=?,window_start=?,window_end=?,
                   timeout_seconds=?,retry_count=?,retry_interval_seconds=?,next_run=?,config_json=?,updated_at=? WHERE key=?""",
                values,
            )
        return self.get(key) or current

    def due(self, now: datetime) -> builtin_list[dict[str, Any]]:
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=? ORDER BY next_run,key",
                (now.isoformat(),),
            ).fetchall()
        return [self._row(row) for row in rows]

    def mark_enqueued(self, task: dict[str, Any], now: datetime) -> None:
        next_run = self._next_run(task["cron"], now, task["weekdays"]).isoformat()
        with self.database.session() as connection:
            connection.execute(
                "UPDATE scheduled_tasks SET next_run=?,error_status=NULL,updated_at=? WHERE key=?",
                (next_run, utc_now(), task["key"]),
            )

    def record_success(self, key: str) -> None:
        with self.database.session() as connection:
            connection.execute(
                "UPDATE scheduled_tasks SET last_success=?,error_status=NULL,updated_at=? WHERE key=?",
                (utc_now(), utc_now(), key),
            )

    def record_failure(self, task: dict[str, Any], message: str, now: datetime) -> None:
        retry_at = now + timedelta(seconds=int(task["retry_interval_seconds"]))
        with self.database.session() as connection:
            connection.execute(
                "UPDATE scheduled_tasks SET last_failure=?,error_status=?,next_run=?,updated_at=? WHERE key=?",
                (utc_now(), message[:1000], retry_at.isoformat(), utc_now(), task["key"]),
            )

    @staticmethod
    def _row(row) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["weekdays"] = json.loads(result.pop("weekdays_json"))
        result["config"] = json.loads(result.pop("config_json"))
        return result

    @staticmethod
    def _bounded(value: Any, lower: int, upper: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("排程數值格式錯誤") from exc
        if not lower <= parsed <= upper:
            raise ValueError(f"排程數值必須介於 {lower}–{upper}")
        return parsed

    @staticmethod
    def _clock(value: str) -> str:
        if len(value) != 5 or value[2] != ":":
            raise ValueError("時間必須是 HH:MM")
        hour, minute = value.split(":")
        if not hour.isdigit() or not minute.isdigit() or not 0 <= int(hour) <= 23 or not 0 <= int(minute) <= 59:
            raise ValueError("時間必須是 HH:MM")
        return value

    def _optional_clock(self, value: Any) -> str | None:
        return None if value in {None, ""} else self._clock(str(value))

    @staticmethod
    def _weekdays(value: Any) -> builtin_list[int]:
        if not isinstance(value, list) or any(not isinstance(day, int) or day < 0 or day > 6 for day in value):
            raise ValueError("星期必須是 0 到 6 的陣列")
        return sorted(set(value))

    @staticmethod
    def _cron(value: str) -> str:
        fields = value.split()
        if len(fields) != 5:
            raise ValueError("Cron 必須是五欄格式")
        for field in fields:
            for part in field.split(","):
                if part == "*" or (part.startswith("*/") and part[2:].isdigit()) or part.isdigit():
                    continue
                raise ValueError("Cron 僅支援 *、數字、逗號與 */間隔")
        return value

    @staticmethod
    def _matches(field: str, value: int) -> bool:
        for part in field.split(","):
            if part == "*" or part == str(value):
                return True
            if part.startswith("*/") and int(part[2:]) > 0 and value % int(part[2:]) == 0:
                return True
        return False

    def _next_run(self, cron: str, after: datetime, weekdays: builtin_list[int]) -> datetime:
        minute, hour, day, month, weekday = cron.split()
        candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(527041):
            cron_weekday = (candidate.weekday() + 1) % 7
            if (
                self._matches(minute, candidate.minute)
                and self._matches(hour, candidate.hour)
                and self._matches(day, candidate.day)
                and self._matches(month, candidate.month)
                and self._matches(weekday, cron_weekday)
                and (not weekdays or candidate.weekday() in weekdays)
            ):
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError("Cron 一年內沒有可執行時間")
