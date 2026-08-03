from __future__ import annotations

import json
from dataclasses import dataclass

from PIL import Image

from inktime.app.services.stock_transport import StockUploadResponse
from tests.conftest import create_admin, csrf, login


@dataclass
class _AcceptedTransport:
    calls: list[tuple[str, bytes]]

    def upload(self, host: str, payload: bytes) -> StockUploadResponse:
        self.calls.append((host, payload))
        return StockUploadResponse(202, {"content-type": "text/plain"}, b"accepted")


def test_admin_stock_display_upload_reports_acceptance_without_claiming_completion(client, app):
    create_admin(app)
    login(client)
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "Stock 相框",
        delivery_mode="stock_compat",
        stock_endpoint_host="display.local",
    )
    staged = app.extensions["inktime_release_publisher"].publish(
        [("stock-photo", Image.new("RGB", (480, 800), "red"))],
        profile_key="safe_4c",
        activate=False,
    )
    release = app.extensions["inktime_release_coordinator"].publish(
        [staged], created_by="stock-api-test", photo_ids=[]
    )[0]
    transport = _AcceptedTransport([])
    app.extensions["inktime_stock_compatibility_service"].transport = transport

    response = client.post(
        f"/api/v1/devices/{device_id}/stock-photopainter/display",
        json={"release_id": release["release_id"], "file_name": release["files"][0]["name"]},
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 202
    body = response.get_json()
    assert body["upload_accepted"] is True
    assert body["display_completed"] is False
    assert body["http_status"] == 202
    assert transport.calls and transport.calls[0][0] == "display.local"
    assert len(transport.calls[0][1]) == 1_152_055
    with app.extensions["inktime_database"].session() as connection:
        event = connection.execute(
            "SELECT event,details_json FROM device_events WHERE device_id=? ORDER BY id DESC LIMIT 1",
            (device_id,),
        ).fetchone()
    assert event["event"] == "stock_upload"
    details = json.loads(event["details_json"])
    assert details["upload_accepted"] is True
    assert details["display_completed"] is False
    assert "Authorization" not in event["details_json"]
    assert "file_path" not in event["details_json"]
