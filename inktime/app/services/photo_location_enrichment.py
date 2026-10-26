"""Persist location enrichment during local scan/analysis, never during render."""
import logging
import sqlite3
import zipfile

from inktime.app.domain.photos.districts import DistrictResolver
from inktime.app.domain.photos.places import PhotoPlace, coordinate_key
from inktime.app.repositories.photo_locations import PhotoLocationRepository


LOGGER = logging.getLogger(__name__)


class PhotoLocationEnrichmentService:
    def __init__(self, database, settings, cities, districts=None):
        self.database = database
        self.settings = settings
        self.repository = PhotoLocationRepository(database)
        self.cities = cities
        self.districts = districts or DistrictResolver()

    def enrich(self, photo):
        if not self.settings.get('location.auto_enrich', True) or not photo['sha256']:
            return None
        if photo['gps_lat'] is None or photo['gps_lon'] is None:
            return None
        try:
            existing = self.repository.get(photo)
            if existing is not None and (existing.district or existing.city or existing.full_address or existing.source == "offline-city-index"):
                return existing
            version = 'offline-nlsc-1120317-cities-v1'
            place = self.repository.cached(photo['gps_lat'], photo['gps_lon'], provider=version, language='zh-Hant')
            if place is None:
                place = self.districts.resolve(photo['gps_lat'], photo['gps_lon'])
                if place is None:
                    city = self.cities.resolve(photo['gps_lat'], photo['gps_lon'])
                    place = PhotoPlace(city=city, source='offline-city-index',
                                       precision='approximate_city' if city else 'unavailable')
                self.repository.cache(photo['gps_lat'], photo['gps_lon'], place, provider=version, language='zh-Hant')
            return self.repository.save_address(photo['id'], photo['sha256'], photo['gps_lat'], photo['gps_lon'], place)
        except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile, KeyError) as error:
            # A stale photo or damaged optional map must not lose a valid scan
            # or trigger a second paid analysis. No GPS is included in logs.
            LOGGER.warning('地區補齊未完成 photo_id=%s reason=%s', photo['id'], type(error).__name__)
            return None

    def enrich_ids(self, ids):
        if not ids or not self.settings.get('location.auto_enrich', True):
            return 0
        import json
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT p.id,p.sha256,p.gps_lat,p.gps_lon,l.gps_key AS cached_gps,l.content_sha256 AS cached_sha,l.place_json AS cached_place FROM photos p "
                "LEFT JOIN photo_locations l ON l.photo_id=p.id "
                "WHERE p.id IN (SELECT value FROM json_each(?)) "
                "AND p.gps_lat IS NOT NULL AND p.gps_lon IS NOT NULL",
                (json.dumps(list(ids)),),
            ).fetchall()
        completed = 0
        for row in rows:
            try:
                if row['cached_sha'] == row['sha256'] and row['cached_gps'] == coordinate_key(row['gps_lat'], row['gps_lon']):
                    cached = json.loads(row['cached_place'] or '{}')
                    if cached.get('district') or cached.get('city') or cached.get('full_address') or cached.get('source') == 'offline-city-index':
                        continue
            except (ValueError, TypeError):
                pass
            completed += self.enrich(row) is not None
        return completed
