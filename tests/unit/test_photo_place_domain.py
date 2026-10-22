from __future__ import annotations

from copy import deepcopy
import json

import pytest
import requests

from inktime.app.domain.analysis.location_experiment import (
    build_location_experiment, incremental_cost, validate_location_experiment,
)
from inktime.app.domain.analysis.schema import json_schema_for_stage
from inktime.app.domain.photos.places import PhotoPlace, coordinate_key, coordinates, from_geoapify, place_date_label
from inktime.app.providers.geocoding import GeocodingError, GeoapifyGeocoder
from tests.unit.test_analysis_schema import valid_result


@pytest.mark.parametrize("lat,lon", [(True, 1), (None, 1), (91, 0), (0, -181), (float("nan"), 0), (0, float("inf")), ("no", 1)])
def test_invalid_coordinates_never_reach_provider(lat, lon):
    with pytest.raises(ValueError):
        coordinates(lat, lon)


def test_cache_key_preserves_exact_coordinates_across_adjacent_buildings():
    assert coordinate_key(25.033001, 121.5654) != coordinate_key(25.033002, 121.5654)
    assert coordinate_key(-0.0, 0) == coordinate_key(0, -0.0)
    assert coordinate_key(None, None) == "none"


def test_address_is_retained_but_only_visual_landmark_or_admin_is_displayed():
    place = PhotoPlace(full_address="新北市新店區示例路1號", district="新店區", city="新北市",
                       landmark="臺北101", landmark_confidence=.89, landmark_source="vision")
    assert place.display_label == "新店區"
    assert PhotoPlace.from_dict(place.as_dict() | {"landmark_confidence": .9}).display_label == "臺北101"
    assert PhotoPlace.from_dict(place.as_dict() | {"landmark_source": "nearby_poi", "landmark_confidence": 1}).display_label == "新店區"
    assert PhotoPlace(full_address="山區道路").display_label == ""
    assert place_date_label("新店區", "2024年7月1日") == "新店區・2024年7月1日"
    assert place_date_label("新店區", "") == "新店區"


@pytest.mark.parametrize("confidence", [True, "0.9", -1, 1.1, float("nan"), float("inf"), 10**1000])
def test_invalid_confidence_is_rejected(confidence):
    with pytest.raises(ValueError):
        PhotoPlace.from_dict({"landmark_confidence": confidence})


def test_reverse_geocoder_never_promotes_nearby_amenity_to_visual_landmark():
    place = from_geoapify({"results": [{"formatted": "臺北市信義區示例路1號", "district": "信義區",
                                       "suburb": "三張里", "city": "臺北市", "name": "臺北101", "result_type": "building"}]})
    assert place.display_label == "信義區"
    assert place.landmark == ""
    assert place.precision == "building"
    assert from_geoapify({"results": []}).precision == "unavailable"
    with pytest.raises(ValueError):
        from_geoapify({"features": []})


def base_request():
    return {"messages": [{"role": "system", "content": "v6 photo rules"},
                         {"role": "user", "content": [{"type": "text", "text": "分析這張照片。"},
                                                         {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,example"}}]}],
            "response_format": {"type": "json_schema", "json_schema": json_schema_for_stage("single")},
            "max_tokens": 1536, "reasoning_effort": "none"}


def test_request_keeps_same_image_and_cost_limits_without_changing_v6_contract():
    original = base_request()
    frozen = deepcopy(original)
    result = build_location_experiment(original, region_hint="臺北市", latitude=25.033, longitude=121.5654)
    assert original == frozen
    assert result["messages"][1]["content"][1] == original["messages"][1]["content"][1]
    assert len(result["messages"][1]["content"]) == 2
    assert result["max_tokens"] == 1536
    assert result["reasoning_effort"] == "none"
    assert "camera_gps" in result["messages"][1]["content"][0]["text"]
    assert result["response_format"]["json_schema"]["schema"]["properties"]["analysis"] == original["response_format"]["json_schema"]["schema"]
    assert "landmark" not in json_schema_for_stage("single")["schema"]["properties"]
    assert "tools" not in result
    assert "camera_gps" not in build_location_experiment(original, region_hint="新店區")["messages"][1]["content"][0]["text"]


def test_landmark_envelope_preserves_safety_and_scores():
    raw = {"analysis": valid_result(), "landmark": {"name": "台北101", "confidence": .95}}
    analysis, landmark = validate_location_experiment(json.dumps(raw))
    assert analysis == valid_result()
    assert landmark["confidence"] == .95
    del raw["analysis"]["display_exclusion"]
    with pytest.raises(ValueError):
        validate_location_experiment(raw)


@pytest.mark.parametrize("landmark", [{"name": "", "confidence": .8}, {"name": "a"*41, "confidence": 1},
                                     {"name": "臺北101", "confidence": True}, {"name": "臺北101"}])
def test_invalid_landmark_cannot_be_imported(landmark):
    with pytest.raises(ValueError):
        validate_location_experiment({"analysis": valid_result(), "landmark": landmark})


def test_additional_reasoning_is_priced_as_output_and_batch_example_is_reproducible():
    assert incremental_cost(photos=100000, input_tokens=200, output_tokens=19, reasoning_tokens=0,
                            input_rate=.10, output_rate=.60) == pytest.approx(3.14)
    assert incremental_cost(photos=100000, input_tokens=200, output_tokens=19, reasoning_tokens=512,
                            input_rate=.10, output_rate=.60) == pytest.approx(33.86)


class Response:
    def __init__(self, payload=None, status=200):
        self.payload = json.dumps(payload if payload is not None else {"results": []}).encode()
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        yield self.payload


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_geocoder_uses_bounded_single_request_and_keeps_provider_key_out_of_errors():
    session = Session(Response())
    geocoder = GeoapifyGeocoder("test-key", session=session)
    assert geocoder.reverse(25.033, 121.5654).precision == "unavailable"
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[0][1]["params"]["lang"] == "zh"
    session.response = requests.Timeout("https://example.invalid/?apiKey=test-key")
    with pytest.raises(GeocodingError) as error:
        geocoder.reverse(25, 121)
    assert "test-key" not in str(error.value)
    assert len(session.calls) == 2
    session.response = Response(status=429)
    with pytest.raises(GeocodingError, match="429"):
        geocoder.reverse(25, 121)
    assert len(session.calls) == 3


def test_geocoder_rejects_oversized_response_without_retry():
    response = Response()
    response.payload = b"x" * 262145
    session = Session(response)
    with pytest.raises(GeocodingError, match="過大"):
        GeoapifyGeocoder("test-key", session=session).reverse(25, 121)
    assert len(session.calls) == 1
