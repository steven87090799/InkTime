from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from typing import Any

from inktime.app.db import Database


_CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class DisplayPrepareConfig:
    display_times: tuple[str, ...]
    lead_minutes: int
    daily_count: int
    device_ids: tuple[str, ...]
    candidate_years: tuple[int, ...]
    prefetch_count: int
    ai_fallback: str
    render_fallback: str

    ALLOWED_KEYS = {
        "display_times",
        "lead_minutes",
        "daily_count",
        "device_ids",
        "candidate_years",
        "prefetch_count",
        "ai_fallback",
        "render_fallback",
    }

    @classmethod
    def from_mapping(cls, value: Any) -> "DisplayPrepareConfig":
        if not isinstance(value, dict):
            raise ValueError("DISPLAY-001 display_prepare 必須是 JSON 物件")
        unknown = sorted(set(value) - cls.ALLOWED_KEYS)
        if unknown:
            raise ValueError(f"DISPLAY-001 不支援的 display_prepare 欄位：{', '.join(unknown)}")

        def bounded_int(key: str, default: int, lower: int, upper: int) -> int:
            raw = value.get(key, default)
            if isinstance(raw, bool):
                raise ValueError(f"DISPLAY-001 {key} 必須是整數")
            try:
                parsed = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"DISPLAY-001 {key} 必須是整數") from exc
            if not lower <= parsed <= upper:
                raise ValueError(f"DISPLAY-001 {key} 必須介於 {lower} 到 {upper}")
            return parsed

        display_times_raw = value.get("display_times", ["08:00"])
        if not isinstance(display_times_raw, list) or not display_times_raw:
            raise ValueError("DISPLAY-001 display_times 必須是非空陣列")
        display_times = tuple(dict.fromkeys(str(item) for item in display_times_raw))
        if any(not _CLOCK.fullmatch(item) for item in display_times):
            raise ValueError("DISPLAY-001 display_times 必須使用 HH:MM")
        daily_count = bounded_int("daily_count", 1, 1, 20)
        if len(display_times) < daily_count:
            raise ValueError("DISPLAY-001 display_times 不得少於 daily_count")

        device_ids_raw = value.get("device_ids", [])
        if not isinstance(device_ids_raw, list) or any(
            not isinstance(item, str) or not item.strip() for item in device_ids_raw
        ):
            raise ValueError("DISPLAY-001 device_ids 必須是裝置 ID 陣列")
        years_raw = value.get("candidate_years", [])
        if not isinstance(years_raw, list):
            raise ValueError("DISPLAY-001 candidate_years 必須是年份陣列")
        years: list[int] = []
        for raw in years_raw:
            if isinstance(raw, bool):
                raise ValueError("DISPLAY-001 candidate_years 必須是年份陣列")
            try:
                year = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("DISPLAY-001 candidate_years 必須是年份陣列") from exc
            if not 1900 <= year <= 2200:
                raise ValueError("DISPLAY-001 candidate_years 超出 1900 到 2200")
            years.append(year)
        ai_fallback = str(value.get("ai_fallback", "use_existing"))
        if ai_fallback not in {"use_existing", "skip", "fail"}:
            raise ValueError("DISPLAY-001 ai_fallback 不支援")
        render_fallback = str(value.get("render_fallback", "keep_current"))
        if render_fallback not in {"keep_current", "fail"}:
            raise ValueError("DISPLAY-001 render_fallback 不支援")
        return cls(
            display_times=display_times,
            lead_minutes=bounded_int("lead_minutes", 30, 0, 1440),
            daily_count=daily_count,
            device_ids=tuple(dict.fromkeys(item.strip() for item in device_ids_raw)),
            candidate_years=tuple(sorted(set(years))),
            prefetch_count=bounded_int("prefetch_count", 1, 1, 10),
            ai_fallback=ai_fallback,
            render_fallback=render_fallback,
        )

    @property
    def output_count(self) -> int:
        return min(50, self.daily_count * self.prefetch_count)

    def target_times(self, target: date) -> tuple[str, ...]:
        return tuple(
            f"{target.isoformat()}T{clock}:00" for clock in self.display_times[: self.daily_count]
        )

    def preparation_times(self, target: date) -> tuple[str, ...]:
        prepared: list[str] = []
        for target_time in self.target_times(target):
            prepared.append(
                (datetime.fromisoformat(target_time) - timedelta(minutes=self.lead_minutes)).isoformat()
            )
        return tuple(prepared)


class DisplayPreparationService:
    def __init__(self, database: Database, render_service) -> None:
        self.database = database
        self.render_service = render_service

    def _profiles(self, config: DisplayPrepareConfig) -> list[str]:
        if not config.device_ids:
            return [str(self.render_service.settings.get("render.profile", "safe_4c"))]
        placeholders = ",".join("?" for _ in config.device_ids)
        with self.database.session() as connection:
            rows = connection.execute(
                f"SELECT id,panel_profile FROM devices WHERE enabled=1 AND id IN ({placeholders})",  # noqa: S608 -- placeholders only
                config.device_ids,
            ).fetchall()
        found = {str(row["id"]): str(row["panel_profile"]) for row in rows}
        missing = [device_id for device_id in config.device_ids if device_id not in found]
        if missing:
            raise ValueError("DISPLAY-004 指定裝置不存在或已停用")
        return list(dict.fromkeys(found[device_id] for device_id in config.device_ids))

    def prepare(self, raw_config: Any, *, created_by: str) -> dict:
        config = DisplayPrepareConfig.from_mapping(raw_config)
        candidates = self.render_service.select_candidates_details(
            config.output_count,
            candidate_years=list(config.candidate_years),
        )
        if not candidates:
            if config.ai_fallback == "skip":
                raise ValueError("DISPLAY-002 AI 尚未完成，排程依設定跳過且未更新成功狀態")
            if config.ai_fallback == "fail":
                raise ValueError("DISPLAY-003 AI 尚未完成，排程依設定失敗")
            raise ValueError("DISPLAY-003 沒有既有且符合資格的分析結果")
        photo_ids = [str(row["id"]) for row in candidates]
        target = self.render_service._today()
        try:
            result = self.render_service.publish(
                photo_ids,
                created_by,
                profile_keys=self._profiles(config),
                history={
                    "history_date": target.isoformat(),
                    "selection_method": "scheduled_display_prepare",
                },
            )
        except Exception as exc:
            if config.render_fallback == "keep_current":
                raise ValueError(
                    "DISPLAY-005 渲染失敗；已保留目前正式 Release，排程未標記成功"
                ) from exc
            raise
        return {
            "release": result,
            "photo_ids": photo_ids,
            "target_display_times": config.target_times(target),
            "preparation_times": config.preparation_times(target),
            "output_count": len(photo_ids),
        }
