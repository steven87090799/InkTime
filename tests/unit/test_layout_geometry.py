from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageChops
import pytest

from inktime.app.domain.rendering.layout_geometry import SUPPORTED_LAYOUTS, resolve_layout_geometry


def _rect(geometry, name):
    value = getattr(geometry, name)
    return value.as_dict() if value is not None else None


def test_baseline_rectangles_match_formal_layout_contract_at_both_orientations():
    expected = {
        "portrait": {
            "full": {
                "primary_photo": {"x": 0, "y": 0, "width": 480, "height": 800},
            },
            "photo_info": {
                "primary_photo": {"x": 0, "y": 0, "width": 480, "height": 704},
                "info_rect": {"x": 0, "y": 704, "width": 480, "height": 96},
            },
            "postcard": {
                "primary_photo": {"x": 24, "y": 24, "width": 432, "height": 634},
                "info_rect": {"x": 24, "y": 658, "width": 432, "height": 142},
            },
            "photo_pair": {
                "primary_photo": {"x": 0, "y": 0, "width": 480, "height": 396},
                "secondary_photo": {"x": 0, "y": 404, "width": 480, "height": 396},
            },
            "photo_pair_caption": {
                "primary_photo": {"x": 0, "y": 0, "width": 480, "height": 317},
                "secondary_photo": {"x": 0, "y": 404, "width": 480, "height": 317},
                "primary_caption": {"x": 0, "y": 317, "width": 480, "height": 79},
                "secondary_caption": {"x": 0, "y": 721, "width": 480, "height": 79},
            },
            "adaptive_memory": {
                "primary_photo": {"x": 0, "y": 0, "width": 480, "height": 704},
                "info_rect": {"x": 0, "y": 704, "width": 480, "height": 96},
            },
            "calendar": {
                "primary_photo": {"x": 20, "y": 312, "width": 440, "height": 420},
                "primary_caption": {"x": 22, "y": 754, "width": 436, "height": 46},
                "info_rect": {"x": 0, "y": 0, "width": 480, "height": 312},
            },
            "weather_sensor": {
                "primary_photo": {"x": 0, "y": 0, "width": 480, "height": 505},
                "primary_caption": {"x": 24, "y": 746, "width": 432, "height": 54},
                "info_rect": {"x": 0, "y": 505, "width": 480, "height": 295},
            },
        },
        "landscape": {
            "full": {
                "primary_photo": {"x": 0, "y": 0, "width": 800, "height": 480},
            },
            "photo_info": {
                "primary_photo": {"x": 0, "y": 0, "width": 800, "height": 404},
                "info_rect": {"x": 0, "y": 404, "width": 800, "height": 76},
            },
            "postcard": {
                "primary_photo": {"x": 24, "y": 24, "width": 752, "height": 334},
                "info_rect": {"x": 24, "y": 358, "width": 752, "height": 122},
            },
            "photo_pair": {
                "primary_photo": {"x": 0, "y": 0, "width": 396, "height": 480},
                "secondary_photo": {"x": 404, "y": 0, "width": 396, "height": 480},
            },
            "photo_pair_caption": {
                "primary_photo": {"x": 0, "y": 0, "width": 396, "height": 384},
                "secondary_photo": {"x": 404, "y": 0, "width": 396, "height": 384},
                "primary_caption": {"x": 0, "y": 384, "width": 396, "height": 96},
                "secondary_caption": {"x": 404, "y": 384, "width": 396, "height": 96},
            },
            "adaptive_memory": {
                "primary_photo": {"x": 0, "y": 0, "width": 800, "height": 404},
                "info_rect": {"x": 0, "y": 404, "width": 800, "height": 76},
            },
            "calendar": {
                "primary_photo": {"x": 20, "y": 312, "width": 440, "height": 420},
                "primary_caption": {"x": 22, "y": 754, "width": 436, "height": 46},
                "info_rect": {"x": 0, "y": 0, "width": 480, "height": 312},
            },
            "weather_sensor": {
                "primary_photo": {"x": 0, "y": 0, "width": 480, "height": 505},
                "primary_caption": {"x": 24, "y": 746, "width": 432, "height": 54},
                "info_rect": {"x": 0, "y": 505, "width": 480, "height": 295},
            },
        },
    }

    for orientation, dimensions in (("portrait", (480, 800)), ("landscape", (800, 480))):
        for layout, rectangles in expected[orientation].items():
            geometry = resolve_layout_geometry(layout, orientation, *dimensions)
            for name, rectangle in rectangles.items():
                assert _rect(geometry, name) == rectangle, (layout, orientation, name)


def test_geometry_normalizes_both_physical_dimension_orders():
    for orientation in ("portrait", "landscape"):
        for dimensions in ((480, 800), (800, 480)):
            geometry = resolve_layout_geometry("photo_info", orientation, *dimensions)
            expected = (480, 800) if orientation == "portrait" else (800, 480)
            assert (geometry.canvas_width, geometry.canvas_height) == expected


def test_every_layout_rectangle_stays_inside_the_canonical_canvas():
    for layout in SUPPORTED_LAYOUTS:
        for orientation, dimensions in (
            ("portrait", (480, 800)),
            ("portrait", (800, 480)),
            ("landscape", (480, 800)),
            ("landscape", (800, 480)),
        ):
            geometry = resolve_layout_geometry(layout, orientation, *dimensions)
            for rectangle in (*geometry.photo_rects, *geometry.caption_rects, *geometry.info):
                assert rectangle.x >= 0 and rectangle.y >= 0
                assert rectangle.width > 0 and rectangle.height > 0
                assert rectangle.right <= geometry.canvas_width
                assert rectangle.bottom <= geometry.canvas_height


@pytest.mark.parametrize("dimensions", [(1, 1), (480, 799), (600, 1000), (0, 800)])
def test_geometry_rejects_noncanonical_dimensions(dimensions):
    with pytest.raises(ValueError, match="480x800"):
        resolve_layout_geometry("full", "portrait", *dimensions)


def test_caption_regions_are_independent_and_keep_text_anchor_pixels():
    geometry = resolve_layout_geometry("photo_pair_caption", "portrait", 480, 800)
    assert geometry.primary_caption.bottom == geometry.primary_photo.bottom + geometry.primary_caption.height
    assert geometry.primary_caption.y == geometry.primary_photo.bottom
    assert geometry.secondary_caption.y == geometry.secondary_photo.bottom
    assert geometry.secondary_caption.bottom == geometry.canvas_height

    postcard = resolve_layout_geometry("postcard", "portrait", 480, 800)
    assert postcard.info_rect.bottom - 48 == 752  # historical caption bottom anchor
    calendar = resolve_layout_geometry("calendar", "portrait", 480, 800)
    assert calendar.primary_caption.y == 754  # historical metadata y
    weather = resolve_layout_geometry("weather_sensor", "portrait", 480, 800)
    assert weather.primary_caption.y == 746  # historical metadata y


def test_portrait_only_layouts_use_portrait_geometry_even_when_requested_landscape():
    for layout in ("calendar", "weather_sensor"):
        geometry = resolve_layout_geometry(layout, "landscape", 800, 480)
        assert geometry.orientation == "portrait"
        assert (geometry.canvas_width, geometry.canvas_height) == (480, 800)


def test_stretch_fill_resizes_exactly_without_letterbox_and_preserves_legacy_modes():
    from inktime.app.services.render_workloads import _photo_renderer_fit
    from inktime.app.services.rendering import RenderService

    source = Image.new("RGB", (2, 4), "red")
    source.putpixel((1, 3), (0, 0, 255))

    fitted, renderer_fit = _photo_renderer_fit(source, "stretch_fill")
    assert fitted.size == (480, 800)
    assert renderer_fit == "cover"
    assert fitted.getpixel((0, 0))[0] > 100
    assert fitted.getpixel((479, 799))[2] > 100

    formal_renderer = object.__new__(RenderService)
    formal = formal_renderer._fit_photo(
        source, {}, (480, 800), None, None, fit_mode="stretch_fill"
    )
    assert formal.size == (480, 800)
    assert formal.getpixel((0, 0))[0] > 100
    assert formal.getpixel((479, 799))[2] > 100

    contained, contain_mode = _photo_renderer_fit(source, "contain")
    covered, cover_mode = _photo_renderer_fit(source, "cover")
    assert contained is source and contain_mode == "contain"
    assert covered is source and cover_mode == "cover"


def test_simulator_caption_freezes_one_legal_font_for_source_and_encoded_outputs(
    monkeypatch, tmp_path: Path
):
    from inktime.app.domain.rendering.fonts import (
        DEFAULT_FONT_ASSET_ROOT,
        FONT_COMPATIBILITY_TEXT,
    )
    from inktime.app.services import render_workloads

    validations = []
    captured = {}

    class RecordingFontManager:
        def __init__(self, *roots):
            self.roots = roots

        def validate_reference(self, reference, text):
            validations.append((reference, text))
            return DEFAULT_FONT_ASSET_ROOT / "Iansui-Regular.ttf"

    def fake_encode(image, **kwargs):
        captured["image"] = image.copy()
        captured["kwargs"] = kwargs
        return SimpleNamespace(payload=b"encoded-payload")

    monkeypatch.setattr(render_workloads, "FontManager", RecordingFontManager)
    monkeypatch.setattr(render_workloads, "encode_image", fake_encode)
    original = Image.new("RGB", (480, 800), "white")
    processed = Image.new("RGB", (480, 800), "white")
    result = SimpleNamespace(
        source=original,
        processed=processed,
        protected_mask=None,
        options={
            "dither": "serpentine_floyd_steinberg",
            "color_distance": "oklab",
            "error_strength": 0.85,
            "linear_light": True,
        },
    )

    source, encoded = render_workloads._captioned_renderer_outputs(
        result,
        settings={
            "caption": "臺北午後",
            "font_reference": "builtin:iansui",
            "font_root": str(tmp_path / "uploaded-fonts"),
            "font_builtin_root": str(DEFAULT_FONT_ASSET_ROOT),
        },
        profile_key="safe_4c",
        profile=object(),
    )

    assert len(validations) == 2
    assert all(reference == "builtin:iansui" for reference, _text in validations)
    assert all(FONT_COMPATIBILITY_TEXT in text for _reference, text in validations)
    assert ImageChops.difference(source, original).getbbox() is not None
    assert ImageChops.difference(captured["image"], processed).getbbox() is not None
    assert captured["kwargs"]["dither"] == "serpentine_floyd_steinberg"
    assert encoded.payload == b"encoded-payload"


def test_stock_test_authorization_is_exactly_bound_and_generic_auth_stays_closed(tmp_path: Path):
    from inktime.app.services.device_releases import DeviceReleaseService

    release_id = "release-stock-direct"
    device_id = "device-stock"
    manifest = {
        "release_id": release_id,
        "release_kind": "device_test",
        "render_profile": "safe_4c",
        "render_options": {
            "transport": "stock_direct",
            "stock_direct": True,
            "stock_direct_device_id": device_id,
        },
    }

    class FakeConnection:
        def __init__(self, device):
            self.device = device

        def execute(self, query, params):
            if "SELECT enabled,delivery_mode,panel_profile" in query:
                return SimpleNamespace(
                    fetchone=lambda: self.device if params[0] == device_id else None
                )
            if "SELECT status FROM releases" in query:
                return SimpleNamespace(fetchone=lambda: {"status": "published"})
            raise AssertionError(f"unexpected query: {query}")

    class FakeDatabase:
        def __init__(self, device):
            self.device = device

        @contextmanager
        def session(self):
            yield FakeConnection(self.device)

    def service_for(device):
        service = object.__new__(DeviceReleaseService)
        service.database = FakeDatabase(device)
        service._load_manifest = lambda _release_id: (tmp_path, (1, 2), dict(manifest))
        service.payload_entry_for_authorization = lambda _authorization: {
            "name": "payload.bin",
            "size": 1,
            "sha256": "0" * 64,
        }
        service.read_payload = lambda _authorization, _filename: (b"x", {})
        return service

    exact = service_for(
        {"enabled": 1, "delivery_mode": "stock_compat", "panel_profile": "safe_4c"}
    ).authorize_stock_test_release_for_device(
        device_id=device_id, profile_key="safe_4c", release_id=release_id
    )
    assert exact.allowed is True
    assert exact.source == "stock_direct_test"
    assert exact.test_assignment is None

    wrong_device = service_for(
        {"enabled": 1, "delivery_mode": "stock_compat", "panel_profile": "safe_4c"}
    ).authorize_stock_test_release_for_device(
        device_id="other-device", profile_key="safe_4c", release_id=release_id
    )
    assert wrong_device.allowed is False

    wrong_profile = service_for(
        {"enabled": 1, "delivery_mode": "stock_compat", "panel_profile": "safe_4c"}
    ).authorize_stock_test_release_for_device(
        device_id=device_id, profile_key="gdep073e01_6c", release_id=release_id
    )
    assert wrong_profile.allowed is False

    for device in (
        {"enabled": 1, "delivery_mode": "custom", "panel_profile": "safe_4c"},
        {"enabled": 0, "delivery_mode": "stock_compat", "panel_profile": "safe_4c"},
    ):
        denied = service_for(device).authorize_stock_test_release_for_device(
            device_id=device_id, profile_key="safe_4c", release_id=release_id
        )
        assert denied.allowed is False

    for invalid_manifest in (
        {**manifest, "release_kind": "formal"},
        {**manifest, "render_options": {"transport": "stock_direct", "stock_direct": True}},
    ):
        service = service_for(
            {"enabled": 1, "delivery_mode": "stock_compat", "panel_profile": "safe_4c"}
        )
        service._load_manifest = lambda _release_id, value=invalid_manifest: (
            tmp_path,
            (1, 2),
            value,
        )
        denied = service.authorize_stock_test_release_for_device(
            device_id=device_id, profile_key="safe_4c", release_id=release_id
        )
        assert denied.allowed is False

    generic = object.__new__(DeviceReleaseService)
    generic._source = lambda **_kwargs: (None, None)
    unchanged = generic.authorize_release_for_device(
        device_id=device_id, profile_key="safe_4c", release_id=release_id
    )
    assert unchanged.allowed is False
    assert unchanged.reason == "not_assigned"


def test_stock_test_release_keeps_exact_source_identity_and_skips_custom_assignment(
    monkeypatch, tmp_path: Path
):
    from inktime.app.services import render_workloads

    token = "0123456789abcdef0123456789abcdef"
    observed = {}

    class Jobs:
        def can_commit_item(self, *args):
            return True

    class Devices:
        def get(self, _device_id):
            return {"enabled": 1, "panel_profile": "safe_4c", "delivery_mode": "stock_compat"}

    class Publisher:
        def find_device_test_by_idempotency(self, _key):
            return None

        def publish_preencoded(self, **kwargs):
            observed["metadata"] = kwargs["metadata"]
            return {
                "release_id": "release-exact-stock",
                "files": [{"name": "payload.bin"}],
            }

    class Boundary:
        def call(self, _function, **kwargs):
            observed["input_path"] = kwargs["kwargs"]["input_path"]
            prepared = Path(kwargs["kwargs"]["prepared_path"])
            prepared.mkdir(parents=True, exist_ok=True)
            (prepared / "payload.bin").write_bytes(b"payload")
            Image.new("RGB", (480, 800), "white").save(prepared / "preview.png")
            return {
                "dither": "serpentine_floyd_steinberg",
                "color_distance": "oklab",
                "dither_strength": 0.85,
                "linear_light": True,
                "palette_version": "stock-test",
                "palette": [],
                "preset": "photo_balanced",
                "source_preset": "photo_balanced",
                "pipeline": {},
                "source_size": "480x800",
                "caption": "臺北午後",
                "font_reference": "builtin:iansui",
            }

    service = render_workloads.RenderWorkloadService(
        tmp_path / "workloads",
        Publisher(),
        Devices(),
        tmp_path / "releases",
        None,
        Boundary(),
        Jobs(),
    )
    deleted = []
    service.delete_input = lambda deleted_token, *, suffix: deleted.append((deleted_token, suffix))
    monkeypatch.setattr(
        render_workloads.DeviceTestReleaseStore,
        "assign",
        lambda *_args, **_kwargs: pytest.fail("Stock direct must not create a Custom assignment"),
    )

    result = service.test_release(
        {
            "input_token": token,
            "input_suffix": ".png",
            "device_id": "stock-device",
            "profile": "safe_4c",
            "transport": "stock_direct",
            "save_preset": False,
            "photo_sha": "source-sha-exact",
            "configuration": {},
        },
        {
            "job_id": "job",
            "item_id": "item",
            "worker_id": "worker",
            "idempotency_key": "idempotency-key",
        },
    )

    assert observed["input_path"] == str(service.input_path(token, suffix=".png"))
    assert observed["metadata"]["source_sha256"] == "source-sha-exact"
    assert observed["metadata"]["caption"] == "臺北午後"
    assert observed["metadata"]["font_reference"] == "builtin:iansui"
    assert observed["metadata"]["transport"] == "stock_direct"
    assert observed["metadata"]["stock_direct"] is True
    assert observed["metadata"]["stock_direct_device_id"] == "stock-device"
    assert result["release_id"] == "release-exact-stock"
    assert result["file_name"] == "payload.bin"
    assert result["stock_touches_custom_queue"] is False
    assert result["stock_touches_custom_ack"] is False
    assert deleted == [(token, ".png")]


def test_stock_display_uploads_the_same_authorized_release_without_completion_claim(
    monkeypatch,
):
    from inktime.app.services.photopainter_stock import StockCompatibilityService

    release_id = "release-exact-stock"
    calls = []

    class Releases:
        def cleanup_expired_stock_test_releases(self):
            calls.append("cleanup")
            return {"examined": 2, "removed": 1}

        def authorize_stock_test_release_for_device(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                allowed=True,
                release_id=release_id,
                manifest={"render_profile": "safe_4c"},
            )

        def payload_entry_for_authorization(self, _authorization):
            return {"name": "payload.bin", "sha256": "source-sha-exact", "size": 6}

        def read_payload(self, _authorization, _file_name):
            return b"packed", {"size": 6, "sha256": "source-sha-exact"}

        def consume_stock_test_release(self, **kwargs):
            calls.append(("consume", kwargs))
            return True

    class Transport:
        def upload(self, host, payload):
            calls.append((host, payload))
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(
        "inktime.app.services.photopainter_stock.packed_frame_to_stock_payload",
        lambda packed, *, profile_key, rotate180: b"stock-payload",
    )
    result = StockCompatibilityService(Releases(), Transport()).display_stock_test_release(
        device_id="stock-device",
        profile_key="safe_4c",
        release_id=release_id,
        file_name="payload.bin",
        host="configured-stock-host",
    )

    assert calls[0] == "cleanup"
    assert calls[1] == {
        "device_id": "stock-device",
        "profile_key": "safe_4c",
        "release_id": release_id,
    }
    assert calls[2] == ("configured-stock-host", b"stock-payload")
    assert calls[3] == (
        "consume",
        {
            "device_id": "stock-device",
            "profile_key": "safe_4c",
            "release_id": release_id,
        },
    )
    assert result["release_id"] == release_id
    assert result["file_name"] == "payload.bin"
    assert result["upload_accepted"] is True
    assert result["display_completed"] is False
    assert result["transport"] == "stock_direct"
    assert result["ephemeral_release_consumed"] is True
    assert result["expired_cleanup_examined"] == 2
    assert result["expired_cleanup_removed"] == 1


class _StockConnection:
    def __init__(self, devices, release_statuses=None, referenced=None):
        self.devices = devices
        self.release_statuses = release_statuses or {}
        self.referenced = set(referenced or ())

    def execute(self, query, params):
        if "SELECT enabled,delivery_mode,panel_profile" in query:
            return SimpleNamespace(fetchone=lambda: self.devices.get(params[0]))
        if "SELECT status FROM releases" in query:
            status = self.release_statuses.get(params[0])
            return SimpleNamespace(
                fetchone=lambda: None if status is None else {"status": status}
            )
        if "SELECT 1 FROM (" in query:
            return SimpleNamespace(
                fetchone=lambda: {"referenced": 1} if params[0] in self.referenced else None
            )
        raise AssertionError(f"unexpected query: {query}")


class _StockDatabase:
    def __init__(self, devices, release_statuses=None, referenced=None):
        self.connection = _StockConnection(devices, release_statuses, referenced)

    @contextmanager
    def session(self):
        yield self.connection


def _write_stock_release(
    release_root: Path,
    release_id: str,
    *,
    device_id: str = "stock-device",
    profile_key: str = "safe_4c",
    expires_at: datetime | None = None,
    manifest_overrides: dict | None = None,
    payload: bytes = b"packed-frame",
    write_payload: bool = True,
    write_marker: bool = True,
):
    expiry = expires_at or datetime.now(timezone.utc) + timedelta(minutes=45)
    release_dir = release_root / release_id
    release_dir.mkdir(parents=True)
    if write_payload:
        (release_dir / "photo_1.bin").write_bytes(payload)
    manifest = {
        "release_id": release_id,
        "release_kind": "device_test",
        "render_profile": profile_key,
        "files": [
            {
                "name": "photo_1.bin",
                "size": len(payload),
                "sha256": sha256(payload).hexdigest(),
            }
        ],
        "render_options": {
            "idempotency_key": f"key-{release_id}",
            "transport": "stock_direct",
            "stock_direct": True,
            "stock_direct_device_id": device_id,
            "stock_direct_expires_at": expiry.isoformat(),
        },
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    (release_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if write_marker:
        marker_root = release_root / ".stock-direct-tests"
        marker_root.mkdir(exist_ok=True)
        (marker_root / f"{release_id}.json").write_text(
            json.dumps(
                {
                    "release_id": release_id,
                    "device_id": device_id,
                    "profile_key": profile_key,
                    "expires_at": expiry.isoformat(),
                }
            ),
            encoding="utf-8",
        )
    return manifest


def _real_stock_release_service(tmp_path: Path, *, statuses=None, referenced=None):
    from inktime.app.services.device_releases import DeviceReleaseService

    devices = {
        "stock-device": {
            "enabled": 1,
            "delivery_mode": "stock_compat",
            "panel_profile": "safe_4c",
        },
        "other-device": {
            "enabled": 1,
            "delivery_mode": "stock_compat",
            "panel_profile": "safe_4c",
        },
        "disabled-device": {
            "enabled": 0,
            "delivery_mode": "stock_compat",
            "panel_profile": "safe_4c",
        },
        "custom-device": {
            "enabled": 1,
            "delivery_mode": "legacy_online",
            "panel_profile": "safe_4c",
        },
    }
    root = tmp_path / "releases"
    root.mkdir(parents=True, exist_ok=True)
    return DeviceReleaseService(
        _StockDatabase(devices, statuses, referenced),
        root,
    )


def test_stock_authorization_allows_filesystem_only_release_and_denies_bad_db_state(tmp_path):
    release_id = "stock-auth-none"
    service = _real_stock_release_service(tmp_path)
    _write_stock_release(service.release_root, release_id)
    allowed = service.authorize_stock_test_release_for_device(
        device_id="stock-device", profile_key="safe_4c", release_id=release_id
    )
    assert allowed.allowed is True

    denied_service = _real_stock_release_service(
        tmp_path / "denied", statuses={release_id: "rendering"}
    )
    _write_stock_release(denied_service.release_root, release_id)
    denied = denied_service.authorize_stock_test_release_for_device(
        device_id="stock-device", profile_key="safe_4c", release_id=release_id
    )
    assert denied.allowed is False
    assert denied.reason == "release_not_downloadable"


@pytest.mark.parametrize(
    ("device_id", "profile_key"),
    [
        ("other-device", "safe_4c"),
        ("stock-device", "gdep073e01_6c"),
        ("disabled-device", "safe_4c"),
        ("custom-device", "safe_4c"),
    ],
)
def test_stock_authorization_denies_wrong_binding_profile_disabled_and_non_stock(
    tmp_path, device_id, profile_key
):
    service = _real_stock_release_service(tmp_path)
    release_id = "stock-auth-denied"
    _write_stock_release(service.release_root, release_id)
    authorization = service.authorize_stock_test_release_for_device(
        device_id=device_id, profile_key=profile_key, release_id=release_id
    )
    assert authorization.allowed is False


@pytest.mark.parametrize(
    "manifest_overrides",
    [
        {"release_kind": "formal"},
        {"render_options": {"transport": "custom", "stock_direct": False}},
        {"render_options": {"transport": "stock_direct", "stock_direct": True}},
    ],
)
def test_stock_authorization_denies_formal_custom_and_missing_binding(
    tmp_path, manifest_overrides
):
    service = _real_stock_release_service(tmp_path)
    release_id = "stock-manifest-denied"
    _write_stock_release(
        service.release_root, release_id, manifest_overrides=manifest_overrides
    )
    authorization = service.authorize_stock_test_release_for_device(
        device_id="stock-device", profile_key="safe_4c", release_id=release_id
    )
    assert authorization.allowed is False


@pytest.mark.parametrize("payload_case", ["missing", "wrong_size", "wrong_sha", "malformed_files"])
def test_stock_payload_integrity_failures_deny_safely_without_iterator_errors(
    tmp_path, payload_case
):
    from inktime.app.services.photopainter_stock import StockCompatibilityService

    service = _real_stock_release_service(tmp_path)
    release_id = f"stock-payload-{payload_case}"
    manifest = _write_stock_release(
        service.release_root,
        release_id,
        write_payload=payload_case != "missing",
    )
    if payload_case == "wrong_size":
        manifest["files"][0]["size"] += 1
    elif payload_case == "wrong_sha":
        manifest["files"][0]["sha256"] = "0" * 64
    elif payload_case == "malformed_files":
        manifest["files"] = [{"name": "photo_1.bin"}]
    (service.release_root / release_id / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    authorization = service.authorize_stock_test_release_for_device(
        device_id="stock-device",
        profile_key="safe_4c",
        release_id=release_id,
    )
    assert authorization.allowed is False
    assert authorization.reason == "invalid_payload"
    with pytest.raises(PermissionError):
        StockCompatibilityService(service).payload_for_stock_test_release(
            device_id="stock-device",
            profile_key="safe_4c",
            release_id=release_id,
        )


@pytest.mark.parametrize("status_code", [400, 500])
def test_stock_non_2xx_retains_ephemeral_release(monkeypatch, tmp_path, status_code):
    from inktime.app.services.photopainter_stock import StockCompatibilityService

    service = _real_stock_release_service(tmp_path)
    release_id = f"stock-http-{status_code}"
    _write_stock_release(service.release_root, release_id)
    monkeypatch.setattr(
        "inktime.app.services.photopainter_stock.packed_frame_to_stock_payload",
        lambda *_args, **_kwargs: b"stock-payload",
    )
    transport = SimpleNamespace(
        upload=lambda _host, _payload: SimpleNamespace(status_code=status_code)
    )
    result = StockCompatibilityService(service, transport).display_stock_test_release(
        device_id="stock-device",
        profile_key="safe_4c",
        release_id=release_id,
        file_name="photo_1.bin",
        host="stock-host",
    )
    assert result["upload_accepted"] is False
    assert result["ephemeral_release_consumed"] is False
    assert (service.release_root / release_id).is_dir()


def test_stock_transport_timeout_retains_ephemeral_release(monkeypatch, tmp_path):
    from inktime.app.services.photopainter_stock import StockCompatibilityService

    service = _real_stock_release_service(tmp_path)
    release_id = "stock-timeout"
    _write_stock_release(service.release_root, release_id)
    monkeypatch.setattr(
        "inktime.app.services.photopainter_stock.packed_frame_to_stock_payload",
        lambda *_args, **_kwargs: b"stock-payload",
    )

    class TimeoutTransport:
        def upload(self, _host, _payload):
            raise TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        StockCompatibilityService(service, TimeoutTransport()).display_stock_test_release(
            device_id="stock-device",
            profile_key="safe_4c",
            release_id=release_id,
            file_name="photo_1.bin",
            host="stock-host",
        )
    assert (service.release_root / release_id).is_dir()


def test_stock_2xx_consumes_exact_ephemeral_release_after_revalidation(monkeypatch, tmp_path):
    from inktime.app.services.photopainter_stock import StockCompatibilityService

    service = _real_stock_release_service(tmp_path)
    release_id = "stock-success"
    _write_stock_release(service.release_root, release_id)
    monkeypatch.setattr(
        "inktime.app.services.photopainter_stock.packed_frame_to_stock_payload",
        lambda *_args, **_kwargs: b"stock-payload",
    )
    transport = SimpleNamespace(upload=lambda *_args: SimpleNamespace(status_code=204))
    result = StockCompatibilityService(service, transport).display_stock_test_release(
        device_id="stock-device",
        profile_key="safe_4c",
        release_id=release_id,
        file_name="photo_1.bin",
        host="stock-host",
    )
    assert result["upload_accepted"] is True
    assert result["ephemeral_release_consumed"] is True
    assert not (service.release_root / release_id).exists()
    assert not (
        service.release_root / ".stock-direct-tests" / f"{release_id}.json"
    ).exists()


@pytest.mark.parametrize(
    "protection",
    ["database", "latest", "legacy_latest", "custom_assignment", "other_assignment"],
)
def test_stock_consume_preserves_any_managed_release_reference(tmp_path, protection):
    release_id = f"stock-protected-{protection}"
    service = _real_stock_release_service(
        tmp_path, referenced={release_id} if protection == "database" else None
    )
    _write_stock_release(service.release_root, release_id)
    if protection == "latest":
        (service.release_root / "latest.safe_4c").write_text(release_id, encoding="utf-8")
    elif protection == "legacy_latest":
        (service.release_root / "latest").write_text(release_id, encoding="utf-8")
    elif protection in {"custom_assignment", "other_assignment"}:
        service.test_store.assign(
            "other-device" if protection == "other_assignment" else "stock-device",
            release_id,
            profile_key="safe_4c",
            delivery="next_wake",
            one_time=True,
            restore_formal=True,
        )
    assert service.consume_stock_test_release(
        device_id="stock-device", profile_key="safe_4c", release_id=release_id
    ) is False
    assert (service.release_root / release_id).is_dir()


def test_stock_cleanup_is_ttl_based_bounded_and_drains_repeated_failures(tmp_path):
    service = _real_stock_release_service(tmp_path)
    now = datetime.now(timezone.utc)
    for index in range(5):
        _write_stock_release(
            service.release_root,
            f"stock-expired-{index}",
            expires_at=now - timedelta(minutes=1),
        )
    _write_stock_release(
        service.release_root,
        "stock-recent",
        expires_at=now + timedelta(minutes=30),
    )
    first = service.cleanup_expired_stock_test_releases(maximum=2, now=now)
    assert first == {"examined": 2, "removed": 2}
    assert (service.release_root / "stock-recent").is_dir()


def test_repeated_expired_stock_tests_are_drained_in_bounded_batches(tmp_path):
    service = _real_stock_release_service(tmp_path)
    now = datetime.now(timezone.utc)
    for index in range(64):
        _write_stock_release(
            service.release_root,
            f"stock-repeated-{index:02d}",
            expires_at=now - timedelta(seconds=1),
        )
    results = [
        service.cleanup_expired_stock_test_releases(maximum=16, now=now)
        for _ in range(4)
    ]
    assert all(result["examined"] <= 16 for result in results)
    assert sum(result["removed"] for result in results) == 64
    assert list(service.release_root.glob("stock-repeated-*")) == []


def test_formal_marker_and_release_id_traversal_fail_closed(tmp_path):
    service = _real_stock_release_service(tmp_path)
    now = datetime.now(timezone.utc)
    formal_id = "formal-preserved"
    _write_stock_release(
        service.release_root,
        formal_id,
        expires_at=now - timedelta(minutes=1),
        manifest_overrides={"release_kind": "formal"},
    )
    result = service.cleanup_expired_stock_test_releases(maximum=8, now=now)
    assert result == {"examined": 1, "removed": 0}
    assert (service.release_root / formal_id).is_dir()

    traversal = service.authorize_stock_test_release_for_device(
        device_id="stock-device",
        profile_key="safe_4c",
        release_id="../outside",
    )
    assert traversal.allowed is False
    assert traversal.reason == "invalid_release_id"
    for _ in range(4):
        service.cleanup_expired_stock_test_releases(maximum=2, now=now)
    remaining = [path.name for path in service.release_root.glob("stock-expired-*")]
    assert remaining == []
    assert (service.release_root / "stock-recent").is_dir()


def test_stock_symlink_and_malformed_manifest_are_preserved_fail_closed(tmp_path):
    service = _real_stock_release_service(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "manifest.json").write_text("{}", encoding="utf-8")
    (service.release_root / "stock-symlink").symlink_to(outside, target_is_directory=True)
    symlink_auth = service.authorize_stock_test_release_for_device(
        device_id="stock-device", profile_key="safe_4c", release_id="stock-symlink"
    )
    assert symlink_auth.allowed is False
    assert outside.is_dir()

    malformed = service.release_root / "stock-malformed"
    malformed.mkdir()
    (malformed / "manifest.json").write_text("{", encoding="utf-8")
    malformed_auth = service.authorize_stock_test_release_for_device(
        device_id="stock-device", profile_key="safe_4c", release_id="stock-malformed"
    )
    assert malformed_auth.allowed is False
    assert malformed.is_dir()

    marker_root = service.release_root / ".stock-direct-tests"
    marker_root.mkdir(exist_ok=True)
    marker_root.rmdir()
    outside_marker_root = tmp_path / "outside-markers"
    outside_marker_root.mkdir()
    outside_marker = outside_marker_root / "do-not-delete.json"
    outside_marker.write_text("{}", encoding="utf-8")
    marker_root.symlink_to(outside_marker_root, target_is_directory=True)
    assert service.cleanup_expired_stock_test_releases() == {
        "examined": 0,
        "removed": 0,
    }
    assert outside_marker.is_file()


def test_stock_device_target_is_frozen_before_await_and_labels_match_contract():
    template = (
        Path(__file__).parents[2] / "inktime/app/web/templates/simulator.html"
    ).read_text(encoding="utf-8")
    function = template.split("async function sendStockTest()", 1)[1].split(
        "async function sendTest()", 1
    )[0]
    before_first_await, after_first_await = function.split("await", 1)
    assert "Object.freeze" in before_first_await
    assert "deviceId:select.value" in before_first_await
    assert "lockedControls=[select,firmware,profile,button]" in before_first_await
    assert "select.value" not in after_first_await
    assert "target.deviceId" in after_first_await
    assert "deterministic sample" not in template.lower()
    assert "deterministic 預設樣本" not in template.lower()
    assert "built-in sample" in template.lower()

    from inktime.app.services.rendering import FIT_MODES

    assert FIT_MODES["stretch_fill"] == "填滿照片區（不裁切，可微變形）"
    assert FIT_MODES["contain"] == "完整顯示"


def test_stock_cleanup_round_robin_eventually_passes_protected_prefix(tmp_path):
    service = _real_stock_release_service(tmp_path)
    now = datetime.now(timezone.utc)
    protected = [f"stock-protected-{index:02d}" for index in range(40)]
    eligible = [f"stock-eligible-{index:02d}" for index in range(20)]
    for release_id in protected:
        _write_stock_release(
            service.release_root,
            release_id,
            expires_at=now + timedelta(hours=1),
        )
    for release_id in eligible:
        _write_stock_release(
            service.release_root,
            release_id,
            expires_at=now - timedelta(minutes=1),
        )

    results = [
        service.cleanup_expired_stock_test_releases(maximum=32, now=now)
        for _ in range(6)
    ]

    assert all(result["examined"] <= 32 for result in results)
    assert all((service.release_root / release_id).is_dir() for release_id in protected)
    assert all(not (service.release_root / release_id).exists() for release_id in eligible)
    state = json.loads(
        (
            service.release_root / ".stock-direct-cleanup-state" / "state.json"
        ).read_text(encoding="utf-8")
    )
    assert state["active"] in {".stock-direct-tests", ".stock-direct-tests-deferred"}


def test_stock_cleanup_cursor_recovers_and_new_or_aging_markers_progress(tmp_path):
    service = _real_stock_release_service(tmp_path)
    now = datetime.now(timezone.utc)
    state_root = service.release_root / ".stock-direct-cleanup-state"
    state_root.mkdir()
    (state_root / "state.json").write_text("{", encoding="utf-8")
    stale_id = "stock-missing-release"
    marker_root = service.release_root / ".stock-direct-tests"
    marker_root.mkdir(exist_ok=True)
    (marker_root / f"{stale_id}.json").write_text(
        json.dumps(
            {
                "release_id": stale_id,
                "device_id": "stock-device",
                "profile_key": "safe_4c",
                "expires_at": (now - timedelta(minutes=1)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    fresh_id = "stock-aging"
    _write_stock_release(
        service.release_root,
        fresh_id,
        expires_at=now + timedelta(minutes=1),
    )
    service.cleanup_expired_stock_test_releases(maximum=4, now=now)
    assert (service.release_root / fresh_id).is_dir()

    added_id = "stock-added-later"
    _write_stock_release(
        service.release_root,
        added_id,
        expires_at=now - timedelta(seconds=1),
    )
    for _ in range(6):
        service.cleanup_expired_stock_test_releases(
            maximum=4, now=now + timedelta(minutes=2)
        )
    assert not (service.release_root / fresh_id).exists()
    assert not (service.release_root / added_id).exists()
    assert list((service.release_root / ".stock-direct-tests-quarantine").iterdir())


@pytest.mark.parametrize("corruption", ["tmp", "invalid_json", "oversized", "symlink"])
def test_corrupt_custom_assignment_is_quarantined_without_global_cleanup_poison(
    tmp_path, corruption
):
    service = _real_stock_release_service(tmp_path)
    now = datetime.now(timezone.utc)
    release_id = f"stock-assignment-{corruption}"
    _write_stock_release(
        service.release_root,
        release_id,
        expires_at=now - timedelta(minutes=1),
    )
    assignment_root = service.test_store.root
    outside = tmp_path / "outside-assignment"
    outside.write_text('{"release_id":"outside"}', encoding="utf-8")
    if corruption == "tmp":
        (assignment_root / "unrelated.tmp").write_text("{", encoding="utf-8")
    elif corruption == "invalid_json":
        (assignment_root / "broken.json").write_text("{", encoding="utf-8")
    elif corruption == "oversized":
        (assignment_root / "broken.json").write_bytes(b"x" * (64 * 1024 + 1))
    else:
        (assignment_root / "broken.json").symlink_to(outside)

    for _ in range(3):
        service.cleanup_expired_stock_test_releases(maximum=4, now=now)

    assert not (service.release_root / release_id).exists()
    assert outside.read_text(encoding="utf-8") == '{"release_id":"outside"}'
    if corruption != "tmp":
        assert list((service.release_root / ".device-tests-quarantine").iterdir())


def test_custom_assignment_snapshot_is_once_per_batch_and_preserves_only_referenced_release(
    monkeypatch, tmp_path
):
    service = _real_stock_release_service(tmp_path)
    now = datetime.now(timezone.utc)
    protected_id = "stock-custom-protected"
    eligible_ids = [f"stock-snapshot-{index:02d}" for index in range(31)]
    for release_id in [protected_id, *eligible_ids]:
        _write_stock_release(
            service.release_root,
            release_id,
            expires_at=now - timedelta(minutes=1),
        )
    service.test_store.assign(
        "other-device",
        protected_id,
        profile_key="safe_4c",
        delivery="next_wake",
        one_time=True,
        restore_formal=True,
    )
    original = service.test_store.reference_snapshot
    scans = 0

    @contextmanager
    def counted_snapshot(*, maximum=1024):
        nonlocal scans
        scans += 1
        with original(maximum=maximum) as snapshot:
            yield snapshot

    monkeypatch.setattr(service.test_store, "reference_snapshot", counted_snapshot)
    result = service.cleanup_expired_stock_test_releases(maximum=32, now=now)

    assert scans == 1
    assert result["examined"] == 32
    assert (service.release_root / protected_id).is_dir()
    assert all(not (service.release_root / release_id).exists() for release_id in eligible_ids)


def test_assignment_snapshot_over_capacity_defers_batch_explicitly(tmp_path):
    service = _real_stock_release_service(tmp_path)
    release_id = "stock-capacity-deferred"
    now = datetime.now(timezone.utc)
    _write_stock_release(
        service.release_root,
        release_id,
        expires_at=now - timedelta(minutes=1),
    )
    for index in range(1025):
        (service.test_store.root / f"device-{index}.json").write_text(
            json.dumps(
                {
                    "device_id": f"device-{index}",
                    "release_id": f"custom-{index}",
                }
            ),
            encoding="utf-8",
        )

    assert service.cleanup_expired_stock_test_releases(now=now) == {
        "examined": 0,
        "removed": 0,
    }
    assert (service.release_root / release_id).is_dir()
