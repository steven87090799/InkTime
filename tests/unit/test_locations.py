from __future__ import annotations

from inktime.app.domain.photos import LocationResolver


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
