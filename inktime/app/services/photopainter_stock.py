"""Stock PhotoPainter delivery adapter.

It is intentionally separate from the production renderer: a release keeps
its panel-native BIN and this service performs the one-way compatibility
conversion only when a Stock device asks for a payload.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from inktime.app.domain.photopainter.stock_protocol import packed_frame_to_stock_payload
from inktime.app.services.device_releases import DeviceReleaseService
from inktime.app.services.stock_transport import StockLanTransport


class StockCompatibilityService:
    def __init__(
        self,
        device_releases: DeviceReleaseService,
        transport: StockLanTransport | None = None,
    ) -> None:
        self.device_releases = device_releases
        self.transport = transport or StockLanTransport()

    def payload_for_release(
        self,
        *,
        device_id: str,
        profile_key: str,
        release_id: str,
        file_name: str | None = None,
        rotate180: bool = False,
    ) -> tuple[bytes, dict[str, Any]]:
        authorization = self.device_releases.authorize_release_for_device(
            device_id=device_id, profile_key=profile_key, release_id=release_id
        )
        if not authorization.allowed or authorization.manifest is None:
            raise PermissionError("PHOTOPAINTER-006 Release 未授權")
        entry = self.device_releases.payload_entry_for_authorization(authorization)
        selected_name = str(file_name or entry["name"])
        if selected_name != str(entry["name"]):
            raise PermissionError("PHOTOPAINTER-006 Release Payload 不在授權 Manifest")
        packed, _metadata = self.device_releases.read_payload(authorization, selected_name)
        manifest = authorization.manifest
        converted = packed_frame_to_stock_payload(
            packed,
            profile_key=str(manifest.get("render_profile") or profile_key),
            rotate180=rotate180,
        )
        return converted, {
            "release_id": release_id,
            "file_name": selected_name,
            "source_sha256": str(entry["sha256"]),
            "stock_sha256": sha256(converted).hexdigest(),
            "source_size": len(packed),
            "size": len(converted),
            "mode": converted[0],
            "content_type": "application/octet-stream",
        }

    def payload_for_latest(
        self,
        *,
        device_id: str,
        profile_key: str,
        rotate180: bool = False,
    ) -> tuple[bytes, dict[str, Any]]:
        authorization = self.device_releases.latest_for_device(
            device_id=device_id, profile_key=profile_key
        )
        if not authorization.allowed:
            raise PermissionError("PHOTOPAINTER-006 沒有可用 Release")
        return self.payload_for_release(
            device_id=device_id,
            profile_key=profile_key,
            release_id=authorization.release_id,
            rotate180=rotate180,
        )

    def display_release(
        self,
        *,
        device_id: str,
        profile_key: str,
        release_id: str,
        file_name: str,
        host: str,
        rotate180: bool = False,
    ) -> dict[str, Any]:
        """Convert and upload exactly one authorized Release to Stock firmware.

        The Stock endpoint has no authenticated completion callback.  A 2xx
        response therefore means only that the upload was accepted by the
        endpoint; the display completion flag intentionally remains false.
        """

        payload, metadata = self.payload_for_release(
            device_id=device_id,
            profile_key=profile_key,
            release_id=release_id,
            file_name=file_name,
            rotate180=rotate180,
        )
        response = self.transport.upload(host, payload)
        return {
            **metadata,
            "upload_accepted": 200 <= response.status_code < 300,
            "display_completed": False,
            "http_status": response.status_code,
        }
