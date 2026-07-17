from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from inktime.app.core.paths import safe_join
from inktime.app.db import Database
from inktime.app.domain.rendering import AtomicReleasePublisher, FontManager
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.repositories.settings import SettingsRepository


class RenderService:
    def __init__(self, database: Database, photos: PhotoRepository, settings: SettingsRepository, fonts: FontManager, publisher: AtomicReleasePublisher) -> None:
        self.database = database
        self.photos = photos
        self.settings = settings
        self.fonts = fonts
        self.publisher = publisher

    def render_photo(self, photo_id: str, width: int = 480, height: int = 800) -> Image.Image:
        photo = self.photos.get_with_path(photo_id)
        if photo is None:
            raise KeyError(photo_id)
        with self.database.session() as connection:
            analysis = connection.execute(
                "SELECT side_caption FROM photo_analysis WHERE photo_id=? ORDER BY created_at DESC LIMIT 1", (photo_id,)
            ).fetchone()
        caption = str(analysis["side_caption"] if analysis else "").strip()
        path = safe_join(Path(photo["root_path"]), photo["relative_path"])
        with Image.open(path) as opened:
            source = ImageOps.exif_transpose(opened).convert("RGB")
            canvas = Image.new("RGB", (width, height), "white")
            text_height = 100 if caption else 0
            fitted = ImageOps.fit(source, (width, height - text_height), method=Image.Resampling.LANCZOS)
            canvas.paste(fitted, (0, 0))
        if caption:
            font_setting = str(self.settings.get("render.font_path", ""))
            font_path = Path(font_setting).expanduser()
            self.fonts.validate(font_path, caption)
            font = ImageFont.truetype(str(font_path), 24)
            draw = ImageDraw.Draw(canvas)
            draw.text((20, height - 68), caption, font=font, fill="black")
        return canvas

    def publish(self, photo_ids: list[str], created_by: str) -> dict:
        quantity = int(self.settings.get("render.quantity", 5))
        selected = photo_ids[:quantity]
        if not selected:
            with self.database.session() as connection:
                selected = [row[0] for row in connection.execute(
                    """
                    SELECT p.id FROM photos p JOIN photo_analysis a ON a.id=(SELECT id FROM photo_analysis WHERE photo_id=p.id ORDER BY created_at DESC LIMIT 1)
                    WHERE p.status='analyzed' ORDER BY a.memory_score DESC LIMIT ?
                    """, (quantity,)).fetchall()]
        images = [(photo_id, self.render_photo(photo_id)) for photo_id in selected]
        manifest = self.publisher.publish(images)
        with self.database.session() as connection:
            connection.execute(
                """
                INSERT INTO releases(id,display_type,width,height,pixel_format,manifest_json,status,created_at,published_at,created_by)
                VALUES (?,?,?,?,?,?,'published',?,?,?)
                """,
                (manifest["release_id"], manifest["display_type"], manifest["width"], manifest["height"],
                 manifest["pixel_format"], json.dumps(manifest, ensure_ascii=False), manifest["created_at"],
                 manifest["created_at"], created_by),
            )
        return manifest

    def rollback(self, release_id: str) -> None:
        self.publisher.rollback(release_id)
        with self.database.session() as connection:
            connection.execute("UPDATE releases SET status=CASE WHEN id=? THEN 'published' ELSE 'superseded' END", (release_id,))
