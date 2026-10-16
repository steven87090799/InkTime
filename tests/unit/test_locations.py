from __future__ import annotations

from inktime.app.domain.photos import LocationResolver
import pytest


@pytest.mark.parametrize("city_lon,photo_lon,latitude", [(179.9, -179.9, 0), (-179.9, 179.9, 0),
                                                        (180, -180, 0), (150, -150, 89.99)])
@pytest.mark.parametrize("grid_degrees", [1.0, 0.25, 7.0])
def test_location_search_wraps_the_dateline_and_searches_all_longitudes_near_poles(tmp_path, city_lon, photo_lon, latitude, grid_degrees):
    path = tmp_path / "cities.csv"
    path.write_text("geonameid,lat,lon,country_code,name_en,name_zh\n"
                    f"1,{latitude},{city_lon},NZ,Nearby,附近\n", encoding="utf-8")
    resolver = LocationResolver(path, grid_degrees=grid_degrees)
    assert resolver.resolve(latitude, photo_lon, max_distance_km=80) == "Nearby"


def test_location_resolver_uses_traditional_chinese_for_taiwan_and_english_elsewhere(tmp_path):
    cities = tmp_path / "cities.csv"
    cities.write_text(
        "geonameid,lat,lon,country_code,name_en,name_zh\n"
        "1,25.05306,121.52639,TW,Taipei,臺北市\n"
        "2,25.07725,55.30927,AE,Dubai,杜拜\n",
        encoding="utf-8",
    )
    resolver = LocationResolver(cities)

    assert resolver.resolve(25.04, 121.53) == "臺北市"
    assert resolver.resolve(25.08, 55.31) == "Dubai"
    assert resolver.resolve(-45.0, -45.0, max_distance_km=20) == ""
