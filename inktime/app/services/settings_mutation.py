"""Atomic application-level settings mutations and runtime side effects."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Callable

from inktime.app.db import Database
from inktime.app.repositories.devices import DeviceRepository
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
        devices: DeviceRepository,
        offline_schedules: OfflineScheduleRepository,
        schedules: ScheduledTaskRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.devices = devices
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
            result, effects = self.update_many_in_transaction(
                connection,
                changed,
                changed_by=changed_by,
                source_ip=source_ip,
                reason=reason,
                rollback_source_snapshot_id=rollback_source_snapshot_id,
            )
        if result["updated"]:
            self.settings.invalidate_runtime_cache()
        return result | {"runtime_effects": effects}

    def update_many_in_transaction(
        self,
        connection,
        updates: dict[str, Any],
        *,
        changed_by: str,
        source_ip: str,
        reason: str | None = None,
        rollback_source_snapshot_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """Persist settings and derived state without committing or touching cache."""

        result = self.settings.update_many_in_transaction(
            connection,
            updates,
            changed_by=changed_by,
            source_ip=source_ip,
            reason=reason,
            rollback_source_snapshot_id=rollback_source_snapshot_id,
        )
        actual = set(result["changed_keys"])
        effects: dict[str, int] = {}
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
        return result, effects

    def apply_preset_atomic(
        self,
        settings: dict[str, Any],
        *,
        device_ids: list[str],
        compatible_panel_profiles: set[str],
        target_panel_profile: str,
        changed_by: str,
        source_ip: str,
        reason: str,
    ) -> dict[str, Any]:
        """Apply selected device overrides and global settings as one operation."""

        _changed, _current, merged = self.settings.prepare_updates(
            settings,
            reject_control_center=True,
        )
        if str(merged.get("device.default_panel_profile")) != target_panel_profile:
            raise ValueError("PRESET-004 Preset 裝置 Profile 與全域設定不一致")
        selected = list(dict.fromkeys(map(str, device_ids)))
        with self.database.transaction(operation="settings_preset_apply") as connection:
            rows = [self.devices.get(device_id, connection=connection) for device_id in selected]
            if any(row is None for row in rows) or any(
                str(row["panel_profile"]) not in compatible_panel_profiles
                for row in rows
                if row is not None
            ):
                raise ValueError("PRESET-004 只能明確更新相容的既有 Spectra 6 裝置")
            changed_devices = 0
            for device_id in selected:
                changed_devices += int(
                    self.devices.update_render_inputs_in_transaction(
                        connection,
                        device_id,
                        panel_profile=target_panel_profile,
                    )
                )
            result, effects = self.update_many_in_transaction(
                connection,
                settings,
                changed_by=changed_by,
                source_ip=source_ip,
                reason=reason,
            )
        if result["updated"]:
            self.settings.invalidate_runtime_cache()
        return result | {
            "runtime_effects": effects,
            "affected_devices": selected,
            "changed_device_count": changed_devices,
        }

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
        with self.database.transaction(operation="settings_snapshot_rollback") as connection:
            preview = self.settings.rollback_preview(snapshot_id, connection=connection)
            result, effects = self.update_many_in_transaction(
                connection,
                preview["updates"],
                changed_by=changed_by,
                source_ip=source_ip,
                reason=reason or f"Rollback 至 Snapshot {snapshot_id}",
                rollback_source_snapshot_id=snapshot_id,
            )
        if result["updated"]:
            self.settings.invalidate_runtime_cache()
        return result | {"runtime_effects": effects}
