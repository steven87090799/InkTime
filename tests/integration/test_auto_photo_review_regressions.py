"""Behavioral regressions found during PR 161 review; uses temporary test data only."""
from __future__ import annotations


import pytest
from PIL import Image, ImageChops, ImageDraw

from tests.conftest import create_admin, login
from tests.integration.test_adaptive_frame_renderer import _analyzed_photo
from inktime.app.services.render_workloads import _prepare_library_preview_child


def _configure(app, **updates):
    actor = create_admin(app)
    app.extensions["inktime_settings_repository"].update_many(
        {"analysis.execution_mode": "local_only", "render.dither": "none", **updates},
        changed_by=actor,
        source_ip="127.0.0.1",
    )
    return actor


@pytest.mark.parametrize("device_specific", [False, True])
def test_manual_auto_publish_reserves_distinct_sources_for_each_frame(app, tmp_path, monkeypatch, device_specific):
    actor = _configure(app, **{"render.layout": "auto_photo", "render.frame_orientation": "landscape"})
    root = tmp_path / "photos"
    root.mkdir()
    ids = [f"portrait-{i}" for i in range(4)]
    for i, photo_id in enumerate(ids):
        _analyzed_photo(app, root, photo_id, (900, 1600), f"2024-07-01T10:0{i}:00+00:00")
    service = app.extensions["inktime_render_service"]
    observed = []

    def capture(plan, **kwargs):
        observed.append(plan)
        return Image.new("RGB", (480, 800), "white")

    monkeypatch.setattr(service, "_render_plan_image", capture)
    device_ids = None
    if device_specific:
        did, _ = app.extensions["inktime_device_repository"].create(
            "Manual automatic frame", panel_profile="safe_4c", layout_mode="auto_photo",
            frame_orientation="landscape",
        )
        device_ids = [did]
    service.publish(ids, actor, profile_keys=["safe_4c"], device_ids=device_ids, quantity_override=2)
    used = [str(p[key]) for p in observed for key in ("primary_photo_id", "secondary_photo_id") if p[key]]
    assert len(observed) == 2
    assert len(used) == len(set(used)) == 4, {"plans": used}


def test_auto_preview_handles_self_as_secondary(app, tmp_path):
    _configure(app)
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "first", (900, 1600), "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "second", (900, 1600), "2024-07-02T10:00:00+00:00")
    client = app.test_client()
    login(client)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    response = client.get("/api/v1/rendering/preview/first?layout=auto_photo&orientation=landscape&secondary_id=first")
    assert response.status_code == 400, {"http_status": response.status_code}


def test_global_auto_layout_change_preserves_release_until_explicit_rerender(app):
    actor = _configure(app, **{"render.layout": "photo_info"})
    devices = app.extensions["inktime_device_repository"]
    did, _ = devices.create(
        "Inherited layout", panel_profile="safe_4c", delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True, schedule_times=["08:00", "20:00"],
        layout_mode=None, frame_orientation="landscape",
    )
    publisher = app.extensions["inktime_release_publisher"]
    coordinator = app.extensions["inktime_release_coordinator"]
    releases = []
    for i in range(2):
        manifest = publisher.publish([(f"old-{i}", Image.new("RGB", (480, 800), "white"))],
                                     profile_key="safe_4c", activate=False)
        releases.append(coordinator.publish([manifest], created_by=actor, photo_ids=[])[0]["release_id"])
    repo = app.extensions["inktime_offline_schedule_repository"]
    before = dict(devices.get(did))
    repo.prepare_day(device_id=did, target_date="2026-10-18", release_ids=releases)
    mutation = app.extensions["inktime_settings_mutation_service"]
    mutation.update_many({"render.layout": "auto_photo"}, changed_by=actor, source_ip="127.0.0.1")
    after = dict(devices.get(did))
    ready = repo.ready_for_device(device_id=did, target_date="2026-10-18", config_version=after["config_version"])
    # The settings contract explicitly says next_render, with existing Releases unchanged.
    assert ready is not None
    assert after["config_version"] == before["config_version"]


@pytest.mark.parametrize("size", [(1600, 900), (900, 1600)])
def test_browser_background_preview_matches_formal_auto_footer(app, tmp_path, size):
    _configure(app)
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "first", size, "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "second", (900, 1600), "2024-07-02T10:00:00+00:00")
    service = app.extensions["inktime_render_service"]
    plan = service.resolve_render_plan(
        "first", layout="auto_photo", orientation="landscape", secondary_photo_id="second",
        fit_mode="cover",
    )
    formal = service._render_plan_image(plan).transpose(Image.Transpose.ROTATE_90)
    arguments = {
        "photo_id": "first", "layout": plan["layout"], "orientation": plan["orientation"],
        "fit_mode": plan["fit_mode"], "primary_caption": plan["primary_caption"],
        "secondary_caption": plan["secondary_caption"], "secondary_photo_id": plan["secondary_photo_id"],
        "calendar_display_date": plan.get("calendar_display_date"), "render_plan": plan,
        "quantized": False,
    }
    prepared = tmp_path / "browser-preview"
    _prepare_library_preview_child(
        settings={"arguments": arguments}, database_path=str(service.database.path),
        prepared_path=str(prepared), font_root=str(service.fonts.root),
        font_builtin_root=str(service.fonts.builtin_root), location_csv=str(service.locations.csv_path),
    )
    with Image.open(prepared / "preview.png") as browser:
        diff = ImageChops.difference(formal.convert("RGB"), browser.convert("RGB"))
        assert diff.getbbox() is None, {"resolved_layout": plan["layout"], "mismatch_box": diff.getbbox()}


def test_auto_preview_handles_mixed_orientation_default_secondary(app, tmp_path):
    _configure(app)
    root = tmp_path / "photos"
    root.mkdir()
    for photo_id, size in (("first", (900, 1600)), ("wrong", (1600, 900)), ("valid", (900, 1600))):
        _analyzed_photo(app, root, photo_id, size, "2024-07-01T10:00:00+00:00")
    client = app.test_client()
    login(client)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    # The composition form initially chooses the next item regardless of its orientation.
    response = client.get("/api/v1/rendering/preview/first?layout=auto_photo&orientation=landscape&secondary_id=wrong")
    assert response.status_code == 400, {"http_status": response.status_code}


@pytest.mark.parametrize("frame", ["portrait", "landscape"])
@pytest.mark.parametrize("size", [(1600, 900), (900, 1600), (1000, 1000)])
@pytest.mark.parametrize("fit", ["contain", "cover"])
@pytest.mark.parametrize("rotation", [0, 90])
def test_automatic_layout_preserves_circle_proportions(app, tmp_path, frame, size, fit, rotation):
    _configure(app)
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "first", size, "2024-07-01T10:00:00+00:00")
    secondary_size = (900, 1600) if frame == "landscape" else (1600, 900)
    _analyzed_photo(app, root, "second", secondary_size, "2024-07-02T10:00:00+00:00")
    with Image.open(root / "first.jpg") as opened:
        image = opened.convert("RGB")
    radius = min(size) // 10
    center = (size[0] // 2, size[1] // 2)
    ImageDraw.Draw(image).ellipse((center[0]-radius, center[1]-radius, center[0]+radius, center[1]+radius), fill="#ff0000")
    image.save(root / "first.jpg")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET manual_orientation_rotation_cw=? WHERE id='first'", (rotation,))
    service = app.extensions["inktime_render_service"]
    effective = size[::-1] if rotation == 90 else size
    is_pair = ((effective[0] > effective[1]) != (frame == "landscape")) and effective[0] != effective[1]
    plan = service.resolve_render_plan(
        "first", layout="auto_photo", orientation=frame,
        secondary_photo_id="second" if is_pair else None,
        fit_mode=fit, crop_x=0.5, crop_y=0.5,
    )
    assert plan["layout"] == ("photo_pair" if is_pair else "full")
    rendered = service._render_plan_image(plan)
    logical = rendered.transpose(Image.Transpose.ROTATE_90) if frame == "landscape" else rendered
    rect = plan["geometry"]["primary_photo"]
    region = logical.crop((rect["x"], rect["y"], rect["x"]+rect["width"], rect["y"]+rect["height"]))
    import numpy as np
    pixels = np.asarray(region)
    ys, xs = np.where((pixels[:,:,0] > 200) & (pixels[:,:,1] < 60) & (pixels[:,:,2] < 60))
    assert xs.size > 0
    width, height = int(xs.max()-xs.min()+1), int(ys.max()-ys.min()+1)
    assert abs(width-height) <= 2, {"circle_bounds": [width, height], "frame": frame, "fit": fit}
