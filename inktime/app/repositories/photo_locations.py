from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from dataclasses import replace
import json
import re

from inktime.app.db import Database
from inktime.app.domain.photos.places import PLACE_VERSION, PhotoPlace, coordinate_key


class PhotoLocationRepository:
    _LANDMARK_FIELDS = frozenset({"landmark", "landmark_confidence", "landmark_source"})

    def __init__(self, database: Database):
        self.database = database

    def get(self, photo) -> PhotoPlace | None:
        if "sha256" not in photo.keys() or not photo["sha256"]:
            return None
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT place_json,gps_key FROM photo_locations WHERE photo_id=? AND content_sha256=?",
                (photo["id"], photo["sha256"]),
            ).fetchone()
        if row is None:
            return None
        try:
            if row["gps_key"] != coordinate_key(photo["gps_lat"], photo["gps_lon"]):
                return None
            return PhotoPlace.from_dict(json.loads(row["place_json"]))
        except (ValueError, TypeError):
            return None

    def save_address(self, photo_id: str, content_sha256: str, latitude, longitude, place: PhotoPlace) -> PhotoPlace:
        # Address providers/cache entries cannot replace photo-specific vision evidence.
        fields = {key: value for key, value in place.as_dict().items() if key not in self._LANDMARK_FIELDS}
        return self._merge(photo_id, content_sha256, latitude, longitude, fields)

    def save_landmark(self, photo_id: str, content_sha256: str, latitude, longitude,
                      *, name: str, confidence: float, connection=None) -> PhotoPlace:
        return self._merge(photo_id, content_sha256, latitude, longitude,
                           {"landmark": name, "landmark_confidence": confidence, "landmark_source": "vision"},
                           connection=connection)

    def _merge(self, photo_id: str, content_sha256: str, latitude, longitude, fields: dict, *, connection=None) -> PhotoPlace:
        if not isinstance(content_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", content_sha256):
            raise ValueError("地點必須綁定照片 SHA-256")
        gps_key = coordinate_key(latitude, longitude)
        normalized = PhotoPlace.from_dict(fields).as_dict()
        updates = {key: normalized[key] for key in fields}
        # BEGIN IMMEDIATE serializes the read and merge, not just the final write.
        # Geocoding/model calls stay outside this short database transaction.
        context = self.database.transaction() if connection is None else nullcontext(connection)
        with context as active_connection:
            connection = active_connection
            photo = connection.execute("SELECT sha256,gps_lat,gps_lon FROM photos WHERE id=?", (photo_id,)).fetchone()
            if photo is None or photo["sha256"] != content_sha256 or coordinate_key(photo["gps_lat"], photo["gps_lon"]) != gps_key:
                raise ValueError("照片內容或 GPS 已改變，拒絕套用舊地點")
            row = connection.execute(
                "SELECT place_json FROM photo_locations WHERE photo_id=? AND content_sha256=? AND gps_key=?",
                (photo_id, content_sha256, gps_key),
            ).fetchone()
            existing = PhotoPlace.from_dict(json.loads(row["place_json"])) if row else PhotoPlace()
            merged = replace(existing, **updates)
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "INSERT INTO photo_locations(photo_id,content_sha256,gps_key,place_json,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(photo_id) DO UPDATE SET content_sha256=excluded.content_sha256,"
                "gps_key=excluded.gps_key,place_json=excluded.place_json,updated_at=excluded.updated_at",
                (photo_id, content_sha256, gps_key, json.dumps(merged.as_dict(), ensure_ascii=False), now),
            )
            if "landmark" in fields:
                evidence = {key: merged.as_dict()[key] for key in self._LANDMARK_FIELDS}
                connection.execute(
                    "UPDATE model_analysis_results SET analysis_snapshot_json=json_set("
                    "analysis_snapshot_json,'$.landmark_evidence',json(?)) WHERE content_sha256=?",
                    (json.dumps(evidence, ensure_ascii=False), content_sha256),
                )
        return merged

    def cached(self, latitude, longitude, *, provider: str, language: str) -> PhotoPlace | None:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT place_json FROM reverse_geocode_cache WHERE gps_key=? AND provider=? AND language=? AND version=?",
                (coordinate_key(latitude, longitude), provider, language, PLACE_VERSION),
            ).fetchone()
        return PhotoPlace.from_dict(json.loads(row["place_json"])) if row else None

    def cache(self, latitude, longitude, place: PhotoPlace, *, provider: str, language: str) -> None:
        # Empty coverage is cached too; transient HTTP failures are never cached.
        address_only = replace(PhotoPlace.from_dict(place.as_dict()), landmark="", landmark_confidence=0,
                               landmark_source="")
        payload = json.dumps(address_only.as_dict(), ensure_ascii=False)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO reverse_geocode_cache(gps_key,provider,language,version,place_json,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(gps_key,provider,language,version) DO UPDATE SET "
                "place_json=excluded.place_json,updated_at=excluded.updated_at",
                (coordinate_key(latitude, longitude), provider, language, PLACE_VERSION,
                 payload, datetime.now(timezone.utc).isoformat()),
            )
