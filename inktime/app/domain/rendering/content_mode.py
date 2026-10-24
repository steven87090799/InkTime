"""Display presentation is independent of background analysis scheduling."""

CONTENT_MODES = frozenset({"photo_only", "photo_caption"})


def content_mode(settings) -> str:
    value = settings.get("render.content_mode", "photo_caption")
    return str(value) if value in CONTENT_MODES else "photo_caption"
