from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import json
from threading import Barrier
from types import SimpleNamespace

import pytest

from inktime.app.domain.photos.places import PhotoPlace
from inktime.app.domain.analysis.scoring import SEMANTIC_SCORE_KIND
from inktime.app.repositories.photo_locations import PhotoLocationRepository
from scripts.experiments.photo_places import enrich, import_landmark
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
    repository.save_address(photo["id"], photo["sha256"], photo["gps_lat"], photo["gps_lon"], place)
    assert repository.get(photo) == place
    changed = dict(photo) | {"gps_lat": 25.033001}
    assert repository.get(changed) is None
    assert repository.get(dict(photo) | {"sha256": "b"*64}) is None
    with pytest.raises(ValueError, match="已改變"):
        repository.save_address(photo["id"], "b"*64, photo["gps_lat"], photo["gps_lon"], place)
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
    repository.save_address(photo["id"], photo["sha256"], photo["gps_lat"], photo["gps_lon"], place)
    repository.save_landmark(photo["id"], photo["sha256"], photo["gps_lat"], photo["gps_lon"],
                             name=place.landmark, confidence=place.landmark_confidence)
    service = app.extensions["inktime_render_service"]
    assert service.location_name(photo) == "臺北101"
    plan = service.resolve_render_plan(photo["id"], layout="auto_photo", orientation="landscape")
    assert plan["capture_date_labels"][photo["id"]] == "2024年7月1日"
    assert plan["photo_footer_parts"][photo["id"]]["location"] == "臺北101"
    assert plan["photo_footer_lines"][photo["id"]].startswith("臺北101・2024年7月1日")
    assert "示例路" not in plan["photo_footer_lines"][photo["id"]]
    repository.save_landmark(photo["id"], photo["sha256"], photo["gps_lat"], photo["gps_lon"],
                             name=place.landmark, confidence=.8)
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
    repository.save_landmark(photo["id"], photo["sha256"], None, None,
                             name=place.landmark, confidence=place.landmark_confidence)
    assert repository.get(photo) == place


@pytest.mark.parametrize("address_first", [False, True])
def test_independent_place_updates_preserve_other_source_and_allow_landmark_clearing(app, tmp_path, address_first):
    photo = seed(app, tmp_path)
    repository = PhotoLocationRepository(app.extensions["inktime_database"])
    identity = (photo["id"], photo["sha256"], photo["gps_lat"], photo["gps_lon"])
    # Simulate an address result built before a newer landmark was imported.
    address = PhotoPlace(full_address="臺北市信義區示例路1號", district="信義區", source="geoapify",
                         landmark="舊地標", landmark_confidence=.99, landmark_source="vision")
    if address_first:
        repository.save_address(*identity, address)
    repository.save_landmark(*identity, name="臺北101", confidence=.95)
    if not address_first:
        repository.save_address(*identity, address)
    expected = replace(address, landmark="臺北101", landmark_confidence=.95)
    assert repository.get(photo) == expected
    cleared = repository.save_landmark(*identity, name="", confidence=0)
    assert cleared == replace(expected, landmark="", landmark_confidence=0)
    assert repository.get(photo) == cleared


def test_concurrent_address_and_landmark_cli_updates_preserve_both_sources(app, tmp_path):
    photo = seed(app, tmp_path)
    database = app.extensions["inktime_database"]
    repository = PhotoLocationRepository(database)
    address = PhotoPlace(full_address="臺北市信義區示例路1號", district="信義區", source="geoapify")
    repository.cache(photo["gps_lat"], photo["gps_lon"], address, provider="geoapify", language="zh")
    result_path = tmp_path / "landmark.json"
    result_path.write_text(json.dumps({"analysis": valid_result(), "landmark": {"name": "臺北101", "confidence": .95}}),
                           encoding="utf-8")
    common = {"database": database.path, "photo_id": photo["id"], "write": True}
    address_args = SimpleNamespace(**common, allow_network=False)
    landmark_args = SimpleNamespace(**common, expected_sha256=photo["sha256"], latitude=photo["gps_lat"],
                                    longitude=photo["gps_lon"], result=result_path)
    start = Barrier(2, timeout=10)

    def update(function, args):
        start.wait()
        return function(args)

    with ThreadPoolExecutor(max_workers=2) as executor:
        address_result = executor.submit(update, enrich, address_args)
        landmark_result = executor.submit(update, import_landmark, landmark_args)
        assert address_result.result(timeout=20)["network_requests"] == 0
        assert landmark_result.result(timeout=20)["photo_id"] == photo["id"]
    assert repository.get(photo) == replace(address, landmark="臺北101", landmark_confidence=.95,
                                            landmark_source="vision")


@pytest.mark.parametrize("changed_field", ["sha256", "gps_lat"])
def test_merge_rejects_old_identity_and_never_carries_evidence_to_changed_photo(app, tmp_path, changed_field):
    photo = seed(app, tmp_path)
    database = app.extensions["inktime_database"]
    repository = PhotoLocationRepository(database)
    identity = (photo["id"], photo["sha256"], photo["gps_lat"], photo["gps_lon"])
    repository.save_address(*identity, PhotoPlace(full_address="舊地址", district="舊行政區"))
    repository.save_landmark(*identity, name="舊地標", confidence=.95)
    with database.transaction() as connection:
        if changed_field == "sha256":
            connection.execute("UPDATE photos SET sha256=? WHERE id=?", ("b" * 64, photo["id"]))
        else:
            connection.execute("UPDATE photos SET gps_lat=? WHERE id=?", (25.04, photo["id"]))
    with pytest.raises(ValueError, match="已改變"):
        repository.save_address(*identity, PhotoPlace(district="過期查詢"))
    with pytest.raises(ValueError, match="已改變"):
        repository.save_landmark(*identity, name="過期辨識", confidence=.99)
    updated = app.extensions["inktime_photo_repository"].get_with_path(photo["id"])
    place = repository.save_address(updated["id"], updated["sha256"], updated["gps_lat"], updated["gps_lon"],
                                     PhotoPlace(district="新行政區"))
    assert place == PhotoPlace(district="新行政區")
    assert repository.get(updated) == place
