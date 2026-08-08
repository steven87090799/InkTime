from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageChops
import pytest

from inktime.app.domain.rendering.layout_geometry import resolve_layout_geometry


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

    assert calls[0] == {
        "device_id": "stock-device",
        "profile_key": "safe_4c",
        "release_id": release_id,
    }
    assert calls[1] == ("configured-stock-host", b"stock-payload")
    assert result["release_id"] == release_id
    assert result["file_name"] == "payload.bin"
    assert result["upload_accepted"] is True
    assert result["display_completed"] is False
    assert result["transport"] == "stock_direct"
