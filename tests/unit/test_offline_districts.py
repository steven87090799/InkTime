from inktime.app.domain.photos.districts import DistrictResolver, contains


def test_public_xindian_station_point_resolves_without_network():
    place = DistrictResolver().resolve(24.9676, 121.5376)
    assert place.district == '新店區'
    assert place.city == '新北市'
    assert place.precision == 'district'


def test_holes_and_exact_boundaries_are_not_guessed():
    rings = [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
             [[3, 3], [7, 3], [7, 7], [3, 7], [3, 3]]]
    assert contains(rings, 1, 1) is True
    assert contains(rings, 5, 5) is False
    assert contains(rings, 0, 5) is None
    assert DistrictResolver().resolve(35.7, 139.7) is None


def test_geometry_cache_and_public_layer_are_bounded():
    resolver = DistrictResolver()
    assert len(resolver.index()['features']) == 369
    assert resolver.geometry.cache_info().maxsize == 8
