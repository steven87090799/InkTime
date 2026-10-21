from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

import pytest

from inktime.app.domain.photos.places import PhotoPlace
from inktime.app.domain.analysis.scoring import SEMANTIC_SCORE_KIND
from inktime.app.repositories.photo_locations import PhotoLocationRepository
from tests.conftest import create_admin, login
from tests.integration.test_adaptive_frame_renderer import _analyzed_photo
from tests.unit.test_analysis_schema import valid_result


def seed(app, tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    _analyzed_photo(app, root, "place-photo", (1600, 960), "2024-07-01T10:00:00+00:00")
    service = app.extensions["inktime_render_service"]
    photo = service.ensure_photo_features("place-photo")
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute("UPDATE photos SET gps_lat=25.033,gps_lon=121.5654,sha256=? WHERE id=?",
                           (sha256((root / "place-photo.jpg").read_bytes()).hexdigest(), photo["id"]))
    app.extensions["inktime_photo_repository"].save_analysis(
        photo["id"], None, "single", "test-ai", "test", valid_result(), "{}",
        score_kind=SEMANTIC_SCORE_KIND, ranking_score=99, final_ranking_score=99,
    )
    return app.extensions["inktime_photo_repository"].get_with_path(photo["id"])


def test_place_is_bound_to_content_and_exact_coordinates(app, tmp_path):
    photo = seed(app, tmp_path)
    repository = PhotoLocationRepository(app.extensions["inktime_database"])
    place = PhotoPlace(full_address="新北市新店區示例路1號", district="新店區", source="geoapify")
    repository.save(photo["id"], photo["sha256"], photo["gps_lat"], photo["gps_lon"], place)
    assert repository.get(photo) == place
    changed = dict(photo) | {"gps_lat": 25.033001}
    assert repository.get(changed) is None
    assert repository.get(dict(photo) | {"sha256": "b"*64}) is None
    with pytest.raises(ValueError, match="已改變"):
        repository.save(photo["id"], "b"*64, photo["gps_lat"], photo["gps_lon"], place)
    repository.cache(photo["gps_lat"], photo["gps_lon"], place, provider="geoapify", language="zh")
    assert repository.cached(photo["gps_lat"], photo["gps_lon"], provider="geoapify", language="zh") == place
    assert repository.cached(25.033001, photo["gps_lon"], provider="geoapify", language="zh") is None
    assert repository.cached(photo["gps_lat"], photo["gps_lon"], provider="geoapify", language="en") is None
    repository.cache(photo["gps_lat"], photo["gps_lon"], replace(place, landmark="臺北101",
                     landmark_confidence=.95, landmark_source="vision"), provider="geoapify", language="zh")
    assert repository.cached(photo["gps_lat"], photo["gps_lon"], provider="geoapify", language="zh").landmark == ""


def test_frame_plan_uses_landmark_and_preserves_capture_date_and_details_address(app, client, tmp_path):
    photo = seed(app, tmp_path)
    repository = PhotoLocationRepository(app.extensions["inktime_database"])
    place = PhotoPlace(full_address="臺北市信義區示例路1號", district="信義區", city="臺北市",
                       source="geoapify", attribution="Geoapify · OpenStreetMap", precision="building",
                       landmark="臺北101", landmark_confidence=.95, landmark_source="vision")
    repository.save(photo["id"], photo["sha256"], photo["gps_lat"], photo["gps_lon"], place)
    service = app.extensions["inktime_render_service"]
    assert service.location_name(photo) == "臺北101"
    plan = service.resolve_render_plan(photo["id"], layout="auto_photo", orientation="landscape")
    assert plan["capture_date_labels"][photo["id"]] == "2024年7月1日"
    assert plan["photo_footer_parts"][photo["id"]]["location"] == "臺北101"
    assert plan["photo_footer_lines"][photo["id"]].startswith("臺北101・2024年7月1日")
    assert "示例路" not in plan["photo_footer_lines"][photo["id"]]
    repository.save(photo["id"], photo["sha256"], photo["gps_lat"], photo["gps_lon"], replace(place, landmark_confidence=.8))
    assert service.location_name(photo) == "信義區"
    create_admin(app)
    login(client)
    response = client.get(f"/photos/{photo['id']}")
    assert response.status_code == 200
    assert "臺北市信義區示例路1號" in response.get_data(as_text=True)
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.show_location", False, changed_by="test", source_ip="local")
    assert service.location_name(photo) == ""


def test_visual_landmark_can_be_saved_without_gps(app, tmp_path):
    photo = seed(app, tmp_path)
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute("UPDATE photos SET gps_lat=NULL,gps_lon=NULL WHERE id=?", (photo["id"],))
    photo = dict(photo) | {"gps_lat": None, "gps_lon": None}
    repository = PhotoLocationRepository(app.extensions["inktime_database"])
    place = PhotoPlace(landmark="中正紀念堂", landmark_confidence=.95, landmark_source="vision")
    repository.save(photo["id"], photo["sha256"], None, None, place)
    assert repository.get(photo) == place
