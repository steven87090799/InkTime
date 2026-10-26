import hashlib


def photo(app, identifier, lat=24.9676, lon=121.5376):
    db = app.extensions['inktime_database']
    with db.transaction() as connection:
        connection.execute("INSERT OR IGNORE INTO libraries(id,name,root_path,created_at,updated_at) "
                           "VALUES ('places','places','/photos','2026-01-01','2026-01-01')")
        connection.execute("INSERT INTO photos(id,library_id,relative_path,sha256,gps_lat,gps_lon,created_at,updated_at) "
                           "VALUES (?,'places',?,?,?,?, '2026-01-01','2026-01-01')",
                           (identifier, identifier + '.jpg', hashlib.sha256(identifier.encode()).hexdigest(), lat, lon))
    return app.extensions['inktime_photo_repository'].get_with_path(identifier)


def test_scan_ids_cache_admin_area_and_reuse_exact_coordinate(app, monkeypatch):
    service = app.extensions['inktime_location_enrichment_service']
    one, two = photo(app, 'one'), photo(app, 'two')
    assert service.enrich_ids(['one']) == 1
    monkeypatch.setattr(service.districts, 'resolve', lambda *_: (_ for _ in ()).throw(AssertionError('cache miss')))
    assert service.enrich_ids(['two']) == 1
    assert service.repository.get(one).district == '新店區'
    assert service.repository.get(two).district == '新店區'
    assert service.enrich_ids(['one', 'two']) == 0


def test_missing_gps_and_optional_map_failure_do_not_fail_photo(app, monkeypatch):
    service = app.extensions['inktime_location_enrichment_service']
    assert service.enrich(photo(app, 'no-gps', None, None)) is None
    row = photo(app, 'map-failure')
    def unavailable(*_):
        raise OSError('optional map unavailable')
    monkeypatch.setattr(service.districts, 'resolve', unavailable)
    assert service.enrich(row) is None


def test_address_enrichment_preserves_landmark_and_rejects_stale_photo(app):
    service = app.extensions['inktime_location_enrichment_service']
    row = photo(app, 'landmark')
    service.repository.save_landmark(row['id'], row['sha256'], row['gps_lat'], row['gps_lon'],
                                     name='已確認地標', confidence=0.95)
    place = service.enrich(row)
    assert place.district == '新店區'
    assert place.landmark == '已確認地標'
    stale = photo(app, 'stale')
    with app.extensions['inktime_database'].transaction() as connection:
        connection.execute("UPDATE photos SET gps_lat=25.0 WHERE id='stale'")
    assert service.enrich(stale) is None
