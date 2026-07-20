from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from inktime.app.core.paths import safe_join
from inktime.app.db import Database
from inktime.app.domain.photos import LocationResolver
from inktime.app.domain.rendering import AtomicReleasePublisher, DISPLAY_PROFILES, FontManager
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.repositories.settings import SettingsRepository


class RenderService:
    def __init__(
        self,
        database: Database,
        photos: PhotoRepository,
        settings: SettingsRepository,
        fonts: FontManager,
        publisher: AtomicReleasePublisher,
        locations: LocationResolver | None = None,
    ) -> None:
        self.database = database
        self.photos = photos
        self.settings = settings
        self.fonts = fonts
        self.publisher = publisher
        self.locations = locations

    def location_name(self, photo) -> str:
        if self.locations is None or not bool(self.settings.get("render.show_location", True)):
            return ""
        return self.locations.resolve(
            photo["gps_lat"],
            photo["gps_lon"],
            max_distance_km=float(self.settings.get("render.location_max_distance_km", 80)),
        )

    @staticmethod
    def _fit_line(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> str:
        if draw.textlength(text, font=font) <= width:
            return text
        suffix = "..."
        fitted = text
        while fitted and draw.textlength(fitted + suffix, font=font) > width:
            fitted = fitted[:-1]
        return fitted.rstrip() + suffix

    def render_photo(self, photo_id: str, width: int = 480, height: int = 800) -> Image.Image:
        photo = self.photos.get_with_path(photo_id)
        if photo is None:
            raise KeyError(photo_id)
        with self.database.session() as connection:
            analysis = connection.execute(
                "SELECT side_caption FROM photo_analysis WHERE photo_id=? ORDER BY created_at DESC LIMIT 1",
                (photo_id,),
            ).fetchone()
        caption = str(analysis["side_caption"] if analysis else "").strip()
        location = self.location_name(photo)
        location_line = f"地點｜{location}" if location else ""
        text_height = 126 if caption and location_line else 92 if caption or location_line else 0
        font_path = None
        caption_font = location_font = None
        if text_height:
            font_setting = str(self.settings.get("render.font_path", ""))
            font_path = self.fonts.resolve(font_setting)
            self.fonts.validate(font_path, "\n".join(value for value in (caption, location_line) if value))
            caption_font = ImageFont.truetype(str(font_path), 24)
            location_font = ImageFont.truetype(str(font_path), 18)
        path = safe_join(Path(photo["root_path"]), photo["relative_path"])
        with Image.open(path) as opened:
            source = ImageOps.exif_transpose(opened).convert("RGB")
            canvas = Image.new("RGB", (width, height), "white")
            fitted = ImageOps.fit(source, (width, height - text_height), method=Image.Resampling.LANCZOS)
            canvas.paste(fitted, (0, 0))
        if text_height and caption_font is not None and location_font is not None:
            draw = ImageDraw.Draw(canvas)
            draw.line((20, height - text_height, width - 20, height - text_height), fill="#d4d0c8", width=1)
            if caption:
                draw.text(
                    (20, height - text_height + 18),
                    self._fit_line(draw, caption, caption_font, width - 40),
                    font=caption_font,
                    fill="black",
                )
            if location_line:
                location_y = height - 38 if caption else height - 58
                draw.text(
                    (20, location_y),
                    self._fit_line(draw, location_line, location_font, width - 40),
                    font=location_font,
                    fill="#454545",
                )
        return canvas

    def publish(
        self,
        photo_ids: list[str],
        created_by: str,
        profile_keys: list[str] | None = None,
    ) -> dict:
        quantity = int(self.settings.get("render.quantity", 5))
        selected = photo_ids[:quantity]
        if not selected:
            selected = self.select_candidates(quantity)
        images = [(photo_id, self.render_photo(photo_id)) for photo_id in selected]
        selected_profiles = profile_keys or [str(self.settings.get("render.profile", "safe_4c"))]
        selected_profiles = list(dict.fromkeys(selected_profiles))
        if not selected_profiles or any(key not in DISPLAY_PROFILES for key in selected_profiles):
            raise ValueError("RENDER-003 發布包含不支援的顯示 Profile")
        dither = str(self.settings.get("render.dither", "floyd_steinberg"))
        color_distance = str(self.settings.get("render.color_distance", "oklab"))
        dither_strength = float(self.settings.get("render.dither_strength", 1.0))
        manifests = []
        for profile_key in selected_profiles:
            manifest = self.publisher.publish(
                images,
                profile_key=profile_key,
                dither=dither,
                color_distance=color_distance,
                dither_strength=dither_strength,
            )
            manifests.append(manifest)
            with self.database.session() as connection:
                connection.execute(
                    """
                    INSERT INTO releases(
                        id,display_type,width,height,pixel_format,manifest_json,status,created_at,
                        published_at,created_by,render_profile
                    ) VALUES (?,?,?,?,?,?,'published',?,?,?,?)
                    """,
                    (
                        manifest["release_id"],
                        manifest["display_type"],
                        manifest["width"],
                        manifest["height"],
                        manifest["pixel_format"],
                        json.dumps(manifest, ensure_ascii=False),
                        manifest["created_at"],
                        manifest["created_at"],
                        created_by,
                        profile_key,
                    ),
                )
        return manifests[0] if len(manifests) == 1 else {"releases": manifests}

    def select_candidates(self, quantity: int | None = None) -> list[str]:
        limit = quantity if quantity is not None else int(self.settings.get("render.quantity", 5))
        memory_threshold = float(self.settings.get("render.memory_threshold", 70))
        with self.database.session() as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT p.id FROM photos p
                    JOIN photo_analysis a ON a.id=(
                        SELECT id FROM photo_analysis WHERE photo_id=p.id ORDER BY created_at DESC LIMIT 1
                    )
                    WHERE p.status='analyzed' AND a.memory_score>=?
                    ORDER BY COALESCE(a.ranking_score,a.memory_score) DESC LIMIT ?
                    """,
                    (memory_threshold, limit),
                ).fetchall()
            ]

    def rollback(self, release_id: str) -> None:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT render_profile FROM releases WHERE id=?", (release_id,)
            ).fetchone()
        if row is None:
            raise KeyError(release_id)
        self.publisher.rollback(release_id)
        with self.database.session() as connection:
            connection.execute(
                """
                UPDATE releases SET status=CASE WHEN id=? THEN 'published' ELSE 'superseded' END
                WHERE render_profile=?
                """,
                (release_id, row["render_profile"]),
            )
