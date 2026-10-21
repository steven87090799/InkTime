from PIL import Image
import pytest

from inktime.app.domain.analysis.scoring import SEMANTIC_SCORE_KIND
from tests.integration.test_dual_photo_caption_layout import _photo
from tests.unit.test_analysis_schema import valid_result


LAYOUTS = ("photo_info", "photo_pair", "photo_pair_caption")


def _photos(app, tmp_path, count):
    root = tmp_path / "layout-photos"
    root.mkdir()
    identifiers = []
    for index, color in enumerate(("#cc2211", "#1133cc", "#22cc44")[:count]):
        identifier = f"layout-{index}"
        _photo(app, root, identifier, (1600, 900), "2020-01-01T00:00:00+00:00", color)
        app.extensions["inktime_photo_repository"].save_analysis(
            identifier, None, "single", "test-ai", "test-model",
            valid_result(types=["其他"]), "{}", score_kind=SEMANTIC_SCORE_KIND,
            ranking_score=80, final_ranking_score=80,
        )
        identifiers.append(identifier)
    return identifiers


@pytest.mark.parametrize("global_layout", LAYOUTS)
@pytest.mark.parametrize("override", [None, *LAYOUTS])
@pytest.mark.parametrize("count", [1, 2, 3])
def test_device_effective_layout_controls_source_count_pairing_and_manifest(app, tmp_path, monkeypatch, global_layout, override, count):
    identifiers = _photos(app, tmp_path, count)
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.layout", global_layout, changed_by="test", source_ip="test")
    device_id, _ = app.extensions["inktime_device_repository"].create("layout", layout_mode=override)
    device = dict(app.extensions["inktime_device_repository"].get(device_id))
    service = app.extensions["inktime_render_service"]
    captured = []
    def record(plan, **kwargs):
        captured.append(plan)
        return Image.new("RGB", (480, 800), "white")
    monkeypatch.setattr(service, "_render_plan_image", record)
    result = service.publish(identifiers, "test", device_ids=[device_id], quantity_override=2)
    effective = override or global_layout
    paired = effective in {"photo_pair", "photo_pair_caption"}
    expected_count = min(2, (count + 1) // 2 if paired else count)
    assert len(captured) == expected_count
    for index, plan in enumerate(captured):
        offset = index * 2 if paired else index
        secondary = identifiers[offset + 1] if paired and offset + 1 < count else None
        assert plan["primary_photo_id"] == identifiers[offset]
        assert plan["secondary_photo_id"] == secondary
        assert plan["layout"] == (effective if not paired or secondary else "photo_info")
        preview = service.preview_fingerprint(identifiers[offset], layout=effective, secondary_photo_id=secondary,
                                               profile=device["panel_profile"])["render_plan"]
        assert preview["layout"] == plan["layout"] and preview["secondary_photo_id"] == secondary
    manifest = app.extensions["inktime_release_publisher"].validate(result["device_releases"][device_id])
    plans = manifest["render_options"]["render_plans"]
    assert [(plan["layout"], plan["primary_photo_id"], plan["secondary_photo_id"]) for plan in plans] == [
        (plan["layout"], plan["primary_photo_id"], plan["secondary_photo_id"]) for plan in captured]


@pytest.mark.parametrize("global_layout", LAYOUTS)
def test_mixed_devices_select_enough_candidates_before_truncation_and_show_real_second_photo(app, tmp_path, monkeypatch, global_layout):
    identifiers = _photos(app, tmp_path, 3)
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.layout", global_layout, changed_by="test", source_ip="test")
    devices = app.extensions["inktime_device_repository"]
    single, _ = devices.create("single", layout_mode="photo_info")
    pair, _ = devices.create("pair", layout_mode="photo_pair_caption")
    service = app.extensions["inktime_render_service"]
    selected_limits = []
    def select(limit):
        selected_limits.append(limit)
        return identifiers[:limit]
    monkeypatch.setattr(service, "select_candidates", select)
    captured = {}
    original = service._render_plan_image
    def render(plan, **kwargs):
        image = original(plan, **kwargs)
        copied = image.copy()
        captured.setdefault(kwargs["device_config"]["id"], []).append((plan, copied))
        return image
    monkeypatch.setattr(service, "_render_plan_image", render)
    result = service.publish([], "test", device_ids=[single, pair], quantity_override=2)
    assert selected_limits == [4]
    assert [plan["secondary_photo_id"] for plan, _ in captured[single]] == [None, None]
    assert [plan["secondary_photo_id"] for plan, _ in captured[pair]] == [identifiers[1], None]
    assert [plan["layout"] for plan, _ in captured[pair]] == ["photo_pair_caption", "photo_info"]
    def blue_pixels(image):
        return sum(b > r + 70 and b > g + 50 for r, g, b in image.convert("RGB").getdata())
    assert blue_pixels(captured[pair][0][1]) > 5000
    assert blue_pixels(captured[single][0][1]) == 0
    for device_id in (single, pair):
        manifest = app.extensions["inktime_release_publisher"].validate(result["device_releases"][device_id])
        assert len(manifest["files"]) == 2
