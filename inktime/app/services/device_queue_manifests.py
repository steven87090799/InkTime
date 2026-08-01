from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from inktime.app.core.paths import UnsafePathError
from inktime.app.repositories.resilience import ResilienceRepository
from inktime.app.services.device_releases import DeviceReleaseService


_ADVERTISED_QUEUE_STATES = {"READY", "AVAILABLE", "DOWNLOADED", "ACKNOWLEDGED"}


class DeviceQueueManifestService:
    """Compose queue rows with the centralized release authorization policy."""

    def __init__(
        self,
        repository: ResilienceRepository,
        release_service: DeviceReleaseService,
        observability: Any | None = None,
    ) -> None:
        self.repository = repository
        self.release_service = release_service
        self.observability = observability

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _audit_skip(self, *, device_id: str, queue_item_id: str, reason: str) -> None:
        if self.observability is None:
            return
        try:
            self.observability.record(
                "WARNING",
                "device_queue",
                "queue_manifest_item_skipped",
                "Queue Manifest 略過未授權或無效項目",
                device_id=device_id,
                queue_item_id=queue_item_id,
                reason=reason[:80],
            )
        except Exception:  # audit failure must not expose internals or fail device delivery
            return

    def build_manifest(self, *, device_id: str, profile_key: str) -> dict[str, Any]:
        queue = self.repository.queue(device_id)
        now = self._now()
        if queue is None:
            return {
                "schema_version": 1,
                "queue_version": 0,
                "device_id": device_id,
                "generated_at": now,
                "items": [],
                "last_known_good_release_id": None,
            }

        items: list[dict[str, Any]] = []
        for row in queue["items"]:
            item_id = str(row["id"])
            if row["status"] not in _ADVERTISED_QUEUE_STATES:
                continue
            if row["expires_at"] and str(row["expires_at"]) <= now:
                continue
            authorization = self.release_service.authorize_release_for_device(
                device_id=device_id,
                profile_key=profile_key,
                release_id=str(row["release_id"]),
            )
            if not authorization.allowed:
                self._audit_skip(
                    device_id=device_id,
                    queue_item_id=item_id,
                    reason=str(authorization.reason or "not_authorized"),
                )
                continue
            try:
                payload = self.release_service.payload_entry_for_authorization(authorization)
            except (OSError, PermissionError, UnsafePathError, ValueError):
                self._audit_skip(
                    device_id=device_id,
                    queue_item_id=item_id,
                    reason="invalid_payload",
                )
                continue
            filename = str(payload["name"])
            items.append(
                {
                    "queue_item_id": item_id,
                    "release_id": str(row["release_id"]),
                    "display_after": row["display_after"],
                    "expires_at": row["expires_at"],
                    "priority": row["priority"],
                    "sha256": payload["sha256"],
                    "size": payload["size"],
                    "download_url": (
                        f"/api/device/v1/queue/items/{quote(item_id, safe='')}/files/"
                        f"{quote(filename, safe='')}"
                    ),
                }
            )
        return {
            "schema_version": 1,
            "queue_version": queue["queue"]["queue_version"],
            "device_id": device_id,
            "generated_at": now,
            "items": items,
            "last_known_good_release_id": queue["queue"]["last_known_good_release_id"],
        }
