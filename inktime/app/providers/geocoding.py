"""Explicit experiment calls only; rendering never calls a geocoding service."""
from __future__ import annotations

import json
import requests

from inktime.app.domain.photos.places import PhotoPlace, coordinates, from_geoapify


class GeocodingError(RuntimeError):
    pass


class GeoapifyGeocoder:
    name = "geoapify"
    language = "zh"

    def __init__(self, api_key: str, *, session=None):
        if not api_key.strip():
            raise ValueError("需要 GEOCODING_API_KEY")
        self._api_key = api_key
        self.session = session or requests.Session()

    def reverse(self, latitude: float, longitude: float) -> PhotoPlace:
        lat, lon = coordinates(latitude, longitude)
        # No automatic retry: an unsuccessful/ambiguous response may still
        # have consumed a provider credit. Never include the key-bearing URL
        # in errors or logs, and never follow redirects carrying credentials.
        try:
            with self.session.get(
                "https://api.geoapify.com/v1/geocode/reverse",
                params={"lat": str(lat), "lon": str(lon), "lang": self.language,
                        "format": "json", "limit": "1", "apiKey": self._api_key},
                timeout=(5, 15), stream=True, allow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise GeocodingError(f"地址服務 HTTP {response.status_code}；未自動重試")
                chunks = bytearray()
                for chunk in response.iter_content(8192):
                    chunks.extend(chunk)
                    if len(chunks) > 262144:
                        raise GeocodingError("地址服務回應過大")
                return from_geoapify(json.loads(chunks))
        except (requests.RequestException, ValueError, TypeError, KeyError):
            raise GeocodingError("地址服務失敗；未自動重試，請確認用量後再操作") from None
