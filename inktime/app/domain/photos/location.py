from __future__ import annotations

import csv
from dataclasses import dataclass
import math
from pathlib import Path
from threading import Lock


@dataclass(frozen=True)
class City:
    latitude: float
    longitude: float
    country_code: str
    name_en: str
    name_zh: str


_TRADITIONAL_CHINESE_REGIONS = {"TW", "HK", "MO"}
_TAIWAN_NAME_OVERRIDES = {
    "Banqiao": "板橋",
    "Hengchun": "恆春",
    "Keelung": "基隆",
    "Taibao": "太保",
}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1.0 - value)))


class LocationResolver:
    """以離線城市索引把 GPS 轉為適合公開顯示的粗略地名。"""

    def __init__(self, csv_path: Path, *, grid_degrees: float = 1.0) -> None:
        self.csv_path = csv_path
        self.grid_degrees = max(0.25, float(grid_degrees))
        self._cities: list[City] | None = None
        self._grid: dict[tuple[int, int], list[int]] | None = None
        self._lock = Lock()

    def _grid_key(self, latitude: float, longitude: float) -> tuple[int, int]:
        return (
            math.floor(latitude / self.grid_degrees),
            math.floor(longitude / self.grid_degrees),
        )

    def _load(self) -> tuple[list[City], dict[tuple[int, int], list[int]]]:
        if self._cities is not None and self._grid is not None:
            return self._cities, self._grid
        with self._lock:
            if self._cities is not None and self._grid is not None:
                return self._cities, self._grid
            cities: list[City] = []
            grid: dict[tuple[int, int], list[int]] = {}
            if self.csv_path.is_file():
                with self.csv_path.open("r", encoding="utf-8", newline="") as stream:
                    for row in csv.DictReader(stream):
                        try:
                            city = City(
                                latitude=float(str(row.get("lat", "")).strip()),
                                longitude=float(str(row.get("lon", "")).strip()),
                                country_code=str(row.get("country_code", "")).strip().upper(),
                                name_en=str(row.get("name_en", "")).strip(),
                                name_zh=str(row.get("name_zh", "")).strip(),
                            )
                        except (TypeError, ValueError):
                            continue
                        grid.setdefault(self._grid_key(city.latitude, city.longitude), []).append(
                            len(cities)
                        )
                        cities.append(city)
            self._cities = cities
            self._grid = grid
            return cities, grid

    @staticmethod
    def _display_name(city: City) -> str:
        if city.country_code == "TW":
            return _TAIWAN_NAME_OVERRIDES.get(city.name_en, city.name_zh or city.name_en)
        # 資料集的其他中文名稱混有簡體字；臺港澳以外優先使用原始英文地名，
        # 避免在 zh-Hant-TW 介面輸出未經確認的簡體轉換結果。
        if city.country_code in _TRADITIONAL_CHINESE_REGIONS:
            return city.name_zh or city.name_en
        return city.name_en or city.name_zh

    def resolve(
        self,
        latitude: float | None,
        longitude: float | None,
        *,
        max_distance_km: float = 80.0,
    ) -> str:
        if latitude is None or longitude is None:
            return ""
        lat = float(latitude)
        lon = float(longitude)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return ""
        maximum = max(1.0, min(float(max_distance_km), 500.0))
        cities, grid = self._load()
        if not cities:
            return ""

        lat_cells = math.ceil(maximum / 110.5 / self.grid_degrees) + 1
        longitude_km = max(2.0, 111.0 * abs(math.cos(math.radians(lat))))
        lon_cells = math.ceil(maximum / longitude_km / self.grid_degrees) + 1
        center_lat, center_lon = self._grid_key(lat, lon)
        candidates: list[int] = []
        for delta_lat in range(-lat_cells, lat_cells + 1):
            for delta_lon in range(-lon_cells, lon_cells + 1):
                candidates.extend(grid.get((center_lat + delta_lat, center_lon + delta_lon), ()))

        nearest: City | None = None
        nearest_distance = float("inf")
        for index in candidates:
            city = cities[index]
            distance = _haversine_km(lat, lon, city.latitude, city.longitude)
            if distance < nearest_distance:
                nearest = city
                nearest_distance = distance
        if nearest is None or nearest_distance > maximum:
            return ""
        return self._display_name(nearest)
