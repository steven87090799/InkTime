"""Offline township lookup with bounded geometry caching and no HTTP calls."""
from functools import lru_cache
import json
from pathlib import Path
import zipfile

from .places import PhotoPlace, coordinates


DATA_PATH = Path(__file__).resolve().parents[3] / 'data' / 'taiwan_districts.zip'


def contains(rings, x, y):
    """Even-odd across rings retains holes; exact boundaries are ambiguous."""
    inside = False
    for ring in rings:
        for a, b in zip(ring, ring[1:]):
            ax, ay = a
            bx, by = b
            cross = (x - ax) * (by - ay) - (y - ay) * (bx - ax)
            if (abs(cross) < 1e-11 and min(ax, bx) <= x <= max(ax, bx)
                    and min(ay, by) <= y <= max(ay, by)):
                return None
            if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
                inside = not inside
    return inside


class DistrictResolver:
    def __init__(self, path=DATA_PATH):
        self.path = Path(path)

    @lru_cache(maxsize=1)
    def index(self):
        with zipfile.ZipFile(self.path) as archive:
            value = json.loads(archive.read('index.json'))
        if not 1 <= len(value['features']) <= 500:
            raise ValueError('行政區索引超出範圍')
        return value

    @lru_cache(maxsize=8)
    def geometry(self, name):
        with zipfile.ZipFile(self.path) as archive:
            if archive.getinfo(name).file_size > 16 * 1024 * 1024:
                raise ValueError('行政區界線資料過大')
            return json.loads(archive.read(name))

    def resolve(self, latitude, longitude):
        lat, lon = coordinates(latitude, longitude)
        # Avoid loading the index at all for coordinates outside Taiwan's
        # supplied town layer (including Kinmen, Matsu and Penghu).
        if not (118 <= lon <= 123 and 21 <= lat <= 27):
            return None
        for feature in self.index()['features']:
            left, bottom, right, top = feature['bounds']
            if not (left <= lon <= right and bottom <= lat <= top):
                continue
            match = contains(self.geometry(feature['file']), lon, lat)
            if match is None:
                return PhotoPlace(city=feature['city'], country='臺灣', source=self.index()['version'],
                                  precision='boundary', attribution='內政部國土測繪中心')
            if match:
                return PhotoPlace(district=feature['district'], city=feature['city'], country='臺灣',
                                  source=self.index()['version'], precision='district',
                                  attribution='內政部國土測繪中心')
        return None
