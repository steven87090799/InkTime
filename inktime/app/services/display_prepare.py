from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import re
from typing import Any

from inktime.app.db import Database
from inktime.app.domain.jobs.failure_policy import (
    JobConfigurationError,
    NoContentError,
    NoEligibleCandidatesError,
    JobFailure,
)
from inktime.app.domain.photopainter.offline_schedule import (
    MINIMUM_SCHEDULE_GAP_MINUTES,
    validate_offline_schedule,
)
from inktime.app.domain.rendering.system_presets import DEFAULT_RENDER_PROFILE


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
            raise JobConfigurationError("DISPLAY-001 display_prepare 必須是 JSON 物件")
        unknown = sorted(set(value) - cls.ALLOWED_KEYS)
        if unknown:
            raise JobConfigurationError(
                f"DISPLAY-001 不支援的 display_prepare 欄位：{', '.join(unknown)}"
            )

        def bounded_int(key: str, default: int, lower: int, upper: int) -> int:
            raw = value.get(key, default)
            if type(raw) is not int:
                raise JobConfigurationError(f"DISPLAY-001 {key} 必須是整數")
            if not lower <= raw <= upper:
                raise JobConfigurationError(f"DISPLAY-001 {key} 必須介於 {lower} 到 {upper}")
            return raw

        display_times_raw = value.get("display_times", ["08:00"])
        if not isinstance(display_times_raw, list) or not display_times_raw:
            raise JobConfigurationError("DISPLAY-001 display_times 必須是非空陣列")
        display_times = tuple(dict.fromkeys(str(item) for item in display_times_raw))
        if any(not _CLOCK.fullmatch(item) for item in display_times):
            raise JobConfigurationError("DISPLAY-001 display_times 必須使用 HH:MM")
        daily_count = bounded_int("daily_count", 1, 1, 24)
        if len(display_times) < daily_count:
            raise JobConfigurationError("DISPLAY-001 display_times 不得少於 daily_count")

        device_ids_raw = value.get("device_ids", [])
        if not isinstance(device_ids_raw, list) or any(
            not isinstance(item, str) or not item.strip() for item in device_ids_raw
        ):
            raise JobConfigurationError("DISPLAY-001 device_ids 必須是裝置 ID 陣列")
        years_raw = value.get("candidate_years", [])
        if not isinstance(years_raw, list):
            raise JobConfigurationError("DISPLAY-001 candidate_years 必須是年份陣列")
        years: list[int] = []
        for raw in years_raw:
            if type(raw) is not int:
                raise JobConfigurationError("DISPLAY-001 candidate_years 必須是年份陣列")
            if not 1900 <= raw <= 2200:
                raise JobConfigurationError("DISPLAY-001 candidate_years 超出 1900 到 2200")
            years.append(raw)
        ai_fallback = str(value.get("ai_fallback", "use_existing"))
        if ai_fallback not in {"use_existing", "skip", "fail"}:
            raise JobConfigurationError("DISPLAY-001 ai_fallback 不支援")
        render_fallback = str(value.get("render_fallback", "keep_current"))
        if render_fallback not in {"keep_current", "fail"}:
            raise JobConfigurationError("DISPLAY-001 render_fallback 不支援")
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
        return tuple(f"{target.isoformat()}T{clock}:00" for clock in self.display_times[: self.daily_count])

    def preparation_times(self, target: date) -> tuple[str, ...]:
        prepared: list[str] = []
        for target_time in self.target_times(target):
            prepared.append(
                (datetime.fromisoformat(target_time) - timedelta(minutes=self.lead_minutes)).isoformat()
            )
        return tuple(prepared)


class DisplayPreparationService:
    def __init__(
        self,
        database: Database,
        render_service,
        *,
        resilience_repository=None,
        offline_schedule_repository=None,
    ) -> None:
        self.database = database
        self.render_service = render_service
        self.resilience = resilience_repository
        self.offline_schedules = offline_schedule_repository

    def preview(self, raw_config: Any, *, target_date: date | None = None) -> dict[str, Any]:
        """Return the exact deterministic candidate order without publishing."""

        config = DisplayPrepareConfig.from_mapping(raw_config)
        target = target_date or self.render_service._today()
        candidates = self.render_service.select_candidates_details(
            config.output_count,
            target_date=target,
            candidate_years=list(config.candidate_years),
        )
        target_times = config.target_times(target)
        playlist = [
            {
                "slot_index": index,
                "display_time": target_times[index % len(target_times)],
                "photo_id": str(row["id"]),
                "captured_at": row.get("captured_at"),
                "match_type": row.get("match_type"),
                "score": row.get("combined_score", row.get("local_display_score")),
                "score_components": row.get("score_components", {}),
            }
            for index, row in enumerate(candidates)
        ]
        return {
            "status": "preview",
            "outcome": "ready" if playlist else "no_content",
            "error_code": None if playlist else "NO_CONTENT",
            "message": None if playlist else "目前沒有可顯示照片，請加入照片後重新執行。",
            "target_display_times": list(target_times),
            "preparation_times": list(config.preparation_times(target)),
            "photo_ids": [entry["photo_id"] for entry in playlist],
            "playlist": playlist,
            "output_count": len(playlist),
        }

    def _profiles(self, config: DisplayPrepareConfig) -> list[str]:
        if not config.device_ids:
            return [str(self.render_service.settings.get("render.profile", DEFAULT_RENDER_PROFILE))]
        placeholders = ",".join("?" for _ in config.device_ids)
        with self.database.session() as connection:
            rows = connection.execute(
                f"SELECT id,panel_profile FROM devices WHERE enabled=1 AND id IN ({placeholders})",  # noqa: S608 -- placeholders only
                config.device_ids,
            ).fetchall()
        found = {str(row["id"]): str(row["panel_profile"]) for row in rows}
        missing = [device_id for device_id in config.device_ids if device_id not in found]
        if missing:
            raise JobConfigurationError("DISPLAY-004 指定裝置不存在或已停用")
        return list(dict.fromkeys(found[device_id] for device_id in config.device_ids))

    def prepare(self, raw_config: Any, *, created_by: str) -> dict:
        config = DisplayPrepareConfig.from_mapping(raw_config)
        candidates = self.render_service.select_candidates_details(
            config.output_count,
            candidate_years=list(config.candidate_years),
        )
        if not candidates:
            if config.ai_fallback == "skip":
                raise NoContentError("DISPLAY-002 AI 尚未完成，排程依設定跳過且未更新成功狀態")
            if config.ai_fallback == "fail":
                raise NoContentError("DISPLAY-003 AI 尚未完成，排程依設定失敗")
            target = self.render_service._today()
            return {
                "status": "completed",
                "outcome": "no_content",
                "error_code": "NO_CONTENT",
                "message": "目前沒有可顯示照片，請加入照片後重新執行。",
                "stage": "no_content",
                "photo_ids": [],
                "target_display_times": config.target_times(target),
                "preparation_times": config.preparation_times(target),
                "output_count": 0,
            }
        photo_ids = [str(row["id"]) for row in candidates]
        target = self.render_service._today()
        try:
            publish_kwargs: dict[str, Any] = {
                "history": {
                    "history_date": target.isoformat(),
                    "selection_method": "scheduled_display_prepare",
                }
            }
            if config.device_ids:
                publish_kwargs["device_ids"] = list(config.device_ids)
            else:
                publish_kwargs["profile_keys"] = self._profiles(config)
            result = self.render_service.publish(photo_ids, created_by, **publish_kwargs)
        except Exception as exc:
            if config.render_fallback == "keep_current":
                raise JobFailure(
                    "DISPLAY-005 渲染失敗；已保留目前正式 Release，排程未標記成功",
                    code="DISPLAY-005",
                ) from exc
            raise
        return {
            "release": result,
            "photo_ids": photo_ids,
            "target_display_times": config.target_times(target),
            "preparation_times": config.preparation_times(target),
            "output_count": len(photo_ids),
        }

    @staticmethod
    def _offline_release_id(result: Any, device_id: str) -> str:
        if isinstance(result, dict):
            assignments = result.get("device_releases")
            if isinstance(assignments, dict) and assignments.get(device_id):
                return str(assignments[device_id])
            releases = result.get("releases")
            if isinstance(releases, list) and len(releases) == 1 and isinstance(releases[0], dict):
                release_id = releases[0].get("release_id") or releases[0].get("id")
                if release_id:
                    return str(release_id)
            release_id = result.get("release_id") or result.get("id")
            if release_id:
                return str(release_id)
        raise ValueError("DISPLAY-006 渲染結果沒有可用的 Release ID")

    def prepare_device_day(
        self,
        *,
        device_id: str,
        target_date: str,
        created_by: str,
        expected_config_version: int | None = None,
    ) -> dict[str, Any]:
        """Prepare one composed Release per Enhanced offline schedule slot.

        The scheduler only creates this bounded render job.  Candidate
        selection and Pillow work remain in the worker process, while the
        repository commits the final queue and slot projection atomically.
        """

        if self.offline_schedules is None or self.resilience is None:
            raise JobConfigurationError("DISPLAY-006 Enhanced offline schedule services 未配置")
        try:
            target = date.fromisoformat(str(target_date))
        except ValueError as exc:
            raise JobConfigurationError("DISPLAY-006 target_date 必須是 YYYY-MM-DD") from exc
        with self.database.session() as connection:
            device = connection.execute(
                """
                SELECT *
                FROM devices WHERE id=? AND enabled=1
                """,
                (device_id,),
            ).fetchone()
        if device is None:
            raise JobConfigurationError("DISPLAY-004 指定裝置不存在或已停用")
        if expected_config_version is not None and int(device["config_version"]) != int(
            expected_config_version
        ):
            raise JobConfigurationError("DISPLAY-CONFIG-RACE 裝置設定已變更，拒絕使用舊排程工作")
        snapshot_config_version = int(device["config_version"])
        if str(device["delivery_mode"]) != "inktime_offline_schedule" or not bool(
            device["offline_prefetch_allowed"]
        ):
            raise JobConfigurationError("QUEUE-005 裝置未啟用離線排程或 Prefetch")
        try:
            schedule_times = validate_offline_schedule(
                json.loads(str(device["schedule_times_json"] or "[]")),
                maximum=24,
                minimum_gap_minutes=int(
                    device["minimum_schedule_gap_minutes"] or MINIMUM_SCHEDULE_GAP_MINUTES
                ),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise JobConfigurationError("DISPLAY-006 裝置 schedule_times 不可解析") from exc

        existing = self.offline_schedules.ready_for_device(
            device_id=device_id,
            target_date=target.isoformat(),
            config_version=int(device["config_version"]),
        )
        if existing is not None:
            return {
                "status": "ready",
                "idempotent": True,
                "playlist_version": existing.get("playlist_version", ""),
                "schedule": existing["schedule"],
                "slots": existing["slots"],
            }

        candidates = self.render_service.select_candidates_details(
            len(schedule_times), target_date=target
        )
        unique_candidates = list(
            dict((str(row["id"]), row) for row in candidates).values()
        )
        if len(unique_candidates) < len(schedule_times):
            raise NoEligibleCandidatesError("DISPLAY-003 沒有足夠的既有且符合資格的分析結果")
        self.resilience.ensure_queue(device_id, depth=max(3, len(schedule_times)))

        release_ids: list[str] = []
        try:
            for slot_index, _slot in enumerate(schedule_times):
                candidate = unique_candidates[slot_index]
                result = self.render_service.publish(
                    [str(candidate["id"])],
                    created_by,
                    # Preparation reserves and downloads a formal frame; it
                    # is not evidence that the panel displayed it.
                    history=None,
                    device_ids=[device_id],
                    quantity_override=1,
                    device_configs={device_id: dict(device)},
                    activate_pointers=False,
                    assign_device_releases=False,
                )
                release_ids.append(self._offline_release_id(result, device_id))

            prepared = self.offline_schedules.prepare_day(
                device_id=device_id,
                target_date=target.isoformat(),
                release_ids=release_ids,
                expected_config_version=snapshot_config_version,
            )
        except Exception as exc:
            self.render_service.release_coordinator.abort_staged(
                release_ids,
                f"offline_prepare_failed:{exc.__class__.__name__}:{exc}",
            )
            raise
        return {
            "status": "ready",
            "idempotent": False,
            "playlist_version": prepared.get("playlist_version", ""),
            "schedule": prepared["schedule"],
            "slots": prepared["slots"],
            "target_display_times": [
                f"{target.isoformat()}T{slot}:00" for slot in schedule_times
            ],
            "output_count": len(release_ids),
        }
