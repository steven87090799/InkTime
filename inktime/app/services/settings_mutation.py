"""Atomic application-level settings mutations and runtime side effects."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Callable

from inktime.app.db import Database
from inktime.app.repositories.offline_schedules import OfflineScheduleRepository
from inktime.app.repositories.schedules import ScheduledTaskRepository
from inktime.app.repositories.settings import SettingsRepository


OFFLINE_PREPARE_POLICY_KEYS = frozenset(
    {
        "offline.server_prefetch_margin_minutes",
        "offline.future_schedule_prepare_hour_local",
    }
)


class SettingsMutationService:
    """Keep settings and their runtime scheduling effects in one transaction."""

    def __init__(
        self,
        database: Database,
        settings: SettingsRepository,
        offline_schedules: OfflineScheduleRepository,
        schedules: ScheduledTaskRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.offline_schedules = offline_schedules
        self.schedules = schedules
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def update_many(
        self,
        updates: dict[str, Any],
        *,
        changed_by: str,
        source_ip: str,
        reason: str | None = None,
        rollback_source_snapshot_id: str | None = None,
        reject_control_center: bool = False,
    ) -> dict[str, Any]:
        changed, _current, _merged = self.settings.prepare_updates(
            updates,
            reject_control_center=reject_control_center,
        )
        if not changed:
            return {
                "updated": 0,
                "changed_keys": [],
                "snapshot_id": None,
                "runtime_effects": {},
            }
        effects: dict[str, int] = {}
        with self.database.transaction() as connection:
            result = self.settings.update_many_in_transaction(
                connection,
                changed,
                changed_by=changed_by,
                source_ip=source_ip,
                reason=reason,
                rollback_source_snapshot_id=rollback_source_snapshot_id,
            )
            actual = set(result["changed_keys"])
            if actual & OFFLINE_PREPARE_POLICY_KEYS:
                effects["offline_prepare_deadlines_invalidated"] = (
                    self.offline_schedules.invalidate_prepare_deadlines_for_policy_change(
                        connection=connection
                    )
                )
            if "general.timezone" in actual:
                row = connection.execute(
                    "SELECT value_json FROM settings WHERE key='general.timezone'"
                ).fetchone()
                timezone_name = json.loads(str(row["value_json"]))
                effects["scheduled_tasks_rebased"] = self.schedules.rebase_enabled_next_runs(
                    str(timezone_name),
                    now=self.clock(),
                    connection=connection,
                )
        if result["updated"]:
            self.settings.invalidate_runtime_cache()
        return result | {"runtime_effects": effects}

    def update(
        self,
        key: str,
        value: Any,
        *,
        changed_by: str,
        source_ip: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return self.update_many(
            {key: value},
            changed_by=changed_by,
            source_ip=source_ip,
            reason=reason,
        )

    def rollback(
        self,
        snapshot_id: str,
        *,
        changed_by: str,
        source_ip: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        preview = self.settings.rollback_preview(snapshot_id)
        return self.update_many(
            preview["updates"],
            changed_by=changed_by,
            source_ip=source_ip,
            reason=reason or f"Rollback 至 Snapshot {snapshot_id}",
            rollback_source_snapshot_id=snapshot_id,
        )
