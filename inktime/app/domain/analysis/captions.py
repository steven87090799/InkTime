"""Input limits for the observation caption used by new photo analyses."""
import json
import re
import unicodedata

from inktime.app.domain.photos.dates import parse_photo_date
from inktime.app.domain.photos.places import PhotoPlace


OBSERVATION_PROMPT_VERSION = "photo-analysis-v6-observation-landmarks-20261125"


def observation_caption_enabled(prompt_version: str | None) -> bool:
    return (prompt_version is None or prompt_version == OBSERVATION_PROMPT_VERSION
            or prompt_version.startswith(OBSERVATION_PROMPT_VERSION + "-caption-"))


def allowed_caption_context(context: dict) -> dict[str, str]:
    """Allow only a year-month and a short city name across the provider boundary."""
    result = {}
    month = context.get("capture_year_month")
    if isinstance(month, str) and re.fullmatch(r"(?!0000)[0-9]{4}-(?:0[1-9]|1[0-2])", month):
        result["capture_year_month"] = month
    city = context.get("city")
    if (isinstance(city, str) and 0 < len(city) <= 64 and any(c.isalpha() for c in city)
            and all(c.isalpha() or unicodedata.category(c).startswith("M") or c in " .-'’·・" for c in city)):
        result["city"] = city.strip()
    return result


def photo_caption_context(photo, place: PhotoPlace | None) -> dict[str, str]:
    context = {}
    if photo["capture_date_status"] == "valid":
        source_date = photo["caption_manual_date"]
        if not source_date:
            try:
                exif = json.loads(photo["exif_json"] or "{}")
                source_date = exif.get("DateTimeOriginal") if isinstance(exif, dict) else None
            except (TypeError, ValueError):
                source_date = None
        # A camera's generic DateTime may be an edit timestamp. Use the capture
        # tag or a content-bound manual date, consistent with the displayed date.
        original = parse_photo_date(source_date, warn=False)
        if original and original.isoformat() == photo["captured_date"]:
            context["capture_year_month"] = original.isoformat()[:7]
    if (place and photo["gps_lat"] is not None and photo["gps_lon"] is not None
            and ((place.source.startswith("nlsc-") and place.precision == "district")
                 or (place.source == "geoapify" and place.precision == "city"))):
        context["city"] = place.city
    return allowed_caption_context(context)
