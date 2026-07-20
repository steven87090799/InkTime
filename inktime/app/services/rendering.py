from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from inktime.app.core.paths import safe_join
from inktime.app.db import Database
from inktime.app.domain.photos import LocationResolver
from inktime.app.domain.rendering import (
    AtomicReleasePublisher,
    DISPLAY_PROFILES,
    FontManager,
    analyze_crop_focus,
    current_local_date,
    evaluate_e6_suitability,
    fit_with_focus,
)
from inktime.app.domain.analysis.scoring import (
    calculate_distinguishing_score,
    prepare_score_distribution,
)
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.repositories.settings import SettingsRepository
from inktime.app.services.weather import WeatherService


LAYOUTS = {
    "full": "全版照片",
    "postcard": "明信片",
    "photo_info": "照片＋日期地點",
    "calendar": "月曆相框",
    "weather_sensor": "天氣＋室內溫溼度",
}


class RenderService:
    def __init__(
        self,
        database: Database,
        photos: PhotoRepository,
        settings: SettingsRepository,
        fonts: FontManager,
        publisher: AtomicReleasePublisher,
        locations: LocationResolver | None = None,
        weather: WeatherService | None = None,
    ) -> None:
        self.database = database
        self.photos = photos
        self.settings = settings
        self.fonts = fonts
        self.publisher = publisher
        self.locations = locations
        self.weather = weather

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

    @staticmethod
    def _captured_date(value: Any) -> date | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except ValueError:
            return None

    def _today(self) -> date:
        return current_local_date(str(self.settings.get("general.timezone", "Asia/Taipei")))

    def _fonts(self, text: str) -> dict[str, ImageFont.FreeTypeFont]:
        font_path = self.fonts.resolve(str(self.settings.get("render.font_path", "")))
        self.fonts.validate(font_path, text)
        return {
            "hero": ImageFont.truetype(str(font_path), 44),
            "large": ImageFont.truetype(str(font_path), 32),
            "body": ImageFont.truetype(str(font_path), 24),
            "meta": ImageFont.truetype(str(font_path), 20),
            "small": ImageFont.truetype(str(font_path), 18),
            "tiny": ImageFont.truetype(str(font_path), 15),
        }

    @staticmethod
    def _subject_box(photo) -> tuple[float, float, float, float] | None:
        values = (
            photo["crop_subject_left"],
            photo["crop_subject_top"],
            photo["crop_subject_right"],
            photo["crop_subject_bottom"],
        )
        if any(value is None for value in values):
            return None
        return tuple(float(value) for value in values)  # type: ignore[return-value]

    def _fit_photo(
        self,
        source: Image.Image,
        photo,
        size: tuple[int, int],
        crop_x: float | None,
        crop_y: float | None,
    ) -> Image.Image:
        manual = crop_x is not None or photo["crop_manual_x"] is not None
        focus_x = float(
            crop_x
            if crop_x is not None
            else photo["crop_manual_x"]
            if photo["crop_manual_x"] is not None
            else photo["crop_focus_x"]
            if photo["crop_focus_x"] is not None
            else 0.5
        )
        focus_y = float(
            crop_y
            if crop_y is not None
            else photo["crop_manual_y"]
            if photo["crop_manual_y"] is not None
            else photo["crop_focus_y"]
            if photo["crop_focus_y"] is not None
            else 0.5
        )
        return fit_with_focus(
            source,
            size,
            focus_x=focus_x,
            focus_y=focus_y,
            subject_box=None if manual else self._subject_box(photo),
        )

    def _ensure_render_features(self, photo, path: Path):
        """延遲補算舊照片的本機構圖資料；不呼叫模型，也不改動原始檔。"""
        needs_crop = photo["crop_focus_x"] is None
        needs_e6 = photo["e6_score"] is None
        if not needs_crop and not needs_e6:
            return photo
        with Image.open(path) as opened:
            opened.draft("RGB", (512, 512))
            sample = ImageOps.exif_transpose(opened).convert("RGB")
            sample.thumbnail((512, 512), Image.Resampling.LANCZOS)
            if needs_crop:
                self.photos.update_crop_analysis(str(photo["id"]), analyze_crop_focus(sample))
            if needs_e6:
                self.photos.update_e6_suitability(
                    str(photo["id"]), evaluate_e6_suitability(sample)
                )
        return self.photos.get_with_path(str(photo["id"])) or photo

    def ensure_photo_features(self, photo_id: str):
        """讓舊照片在詳情或渲染頁第一次使用時取得本機構圖資料。"""
        photo = self.photos.get_with_path(photo_id)
        if photo is None:
            raise KeyError(photo_id)
        path = safe_join(Path(photo["root_path"]), photo["relative_path"])
        if not path.is_file():
            return photo
        return self._ensure_render_features(photo, path)

    def _latest_indoor(self) -> dict[str, Any] | None:
        device_id = str(self.settings.get("render.sensor_device_id", "")).strip()
        with self.database.session() as connection:
            if device_id:
                row = connection.execute(
                    """
                    SELECT s.temperature_c,s.humidity_percent,s.recorded_at,d.name device_name
                    FROM device_power_samples s JOIN devices d ON d.id=s.device_id
                    WHERE s.device_id=?
                      AND (s.temperature_c IS NOT NULL OR s.humidity_percent IS NOT NULL)
                    ORDER BY s.recorded_at DESC,s.id DESC LIMIT 1
                    """,
                    (device_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT s.temperature_c,s.humidity_percent,s.recorded_at,d.name device_name
                    FROM device_power_samples s JOIN devices d ON d.id=s.device_id
                    WHERE s.temperature_c IS NOT NULL OR s.humidity_percent IS NOT NULL
                    ORDER BY s.recorded_at DESC,s.id DESC LIMIT 1
                    """
                ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _date_label(captured: date | None) -> str:
        return f"{captured.year}年{captured.month}月{captured.day}日" if captured else "拍攝日期未知"

    def _calendar(self, canvas: Image.Image, fonts, today: date, start_y: int = 75) -> None:
        draw = ImageDraw.Draw(canvas)
        weekdays = "一二三四五六日"
        column_width = 62
        left = 23
        for column, label in enumerate(weekdays):
            draw.text((left + column * column_width + 20, start_y), label, font=fonts["tiny"], fill="#59605a")
        weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(today.year, today.month)
        for row, week in enumerate(weeks):
            for column, day in enumerate(week):
                if day == 0:
                    continue
                x = left + column * column_width
                y = start_y + 28 + row * 36
                if day == today.day:
                    draw.rounded_rectangle((x + 8, y - 3, x + 47, y + 29), radius=12, fill="#d13b2f")
                    color = "white"
                else:
                    color = "#1d2822"
                draw.text((x + 17, y), str(day), font=fonts["tiny"], fill=color)

    def render_photo(
        self,
        photo_id: str,
        width: int = 480,
        height: int = 800,
        *,
        layout: str | None = None,
        crop_x: float | None = None,
        crop_y: float | None = None,
    ) -> Image.Image:
        photo = self.ensure_photo_features(photo_id)
        path = safe_join(Path(photo["root_path"]), photo["relative_path"])
        with self.database.session() as connection:
            analysis = connection.execute(
                "SELECT side_caption FROM photo_analysis WHERE photo_id=? ORDER BY created_at DESC LIMIT 1",
                (photo_id,),
            ).fetchone()
        caption = str(analysis["side_caption"] if analysis else "").strip()
        location = self.location_name(photo)
        captured = self._captured_date(photo["captured_at"])
        show_date = bool(self.settings.get("render.show_capture_date", True))
        date_label = self._date_label(captured) if show_date else ""
        layout_key = layout or str(self.settings.get("render.layout", "photo_info"))
        if layout_key not in LAYOUTS:
            raise ValueError("RENDER-005 不支援的相框版型")
        with Image.open(path) as opened:
            source = ImageOps.exif_transpose(opened).convert("RGB")
            if layout_key == "full":
                return self._fit_photo(source, photo, (width, height), crop_x, crop_y)

            today = self._today()
            weather = self.weather.current() if self.weather and layout_key == "weather_sensor" else None
            indoor = self._latest_indoor() if layout_key == "weather_sensor" else None
            weather_location = str(self.settings.get("render.weather_location_name", "所在地"))
            text_parts = [caption, location, date_label, f"{today.month}月{today.day}日", "星期一二三四五六日"]
            if weather:
                text_parts.extend([str(weather.get("condition", "")), weather_location, "室外室內最高最低溫溼度"])
            if indoor:
                text_parts.extend([str(indoor.get("device_name", "")), "室內溫度濕度"])
            fonts = self._fonts("\n".join(part for part in text_parts if part))
            # 資訊區使用真正的面板白色，避免米白經抖動後變成彩色雜點。
            canvas = Image.new("RGB", (width, height), "white")
            draw = ImageDraw.Draw(canvas)

            if layout_key == "postcard":
                fitted = self._fit_photo(source, photo, (432, 570), crop_x, crop_y)
                canvas.paste(fitted, (24, 24))
                draw.rectangle((23, 23, 456, 595), outline="#b9afa0", width=2)
                if caption:
                    draw.text((28, 625), self._fit_line(draw, caption, fonts["body"], 424), font=fonts["body"], fill="#1b241f")
                meta = "・".join(value for value in (date_label, location) if value)
                draw.text((28, 744), self._fit_line(draw, meta, fonts["small"], 424), font=fonts["small"], fill="#59605a")
                return canvas

            if layout_key == "photo_info":
                # 將資訊帶由 150px 縮為 96px，保留更多照片，同時讓文字使用純黑實色。
                fitted = self._fit_photo(source, photo, (width, 704), crop_x, crop_y)
                canvas.paste(fitted, (0, 0))
                draw.rectangle((0, 704, width, height), fill="white")
                draw.line((20, 708, width - 20, 708), fill="black", width=2)
                if caption:
                    draw.text((22, 716), self._fit_line(draw, caption, fonts["body"], width - 44), font=fonts["body"], fill="black")
                meta = "・".join(value for value in (date_label, location) if value)
                draw.text((22, 768), self._fit_line(draw, meta, fonts["meta"], width - 44), font=fonts["meta"], fill="black")
                return canvas

            if layout_key == "calendar":
                draw.text((24, 16), f"{today.year}年 {today.month}月", font=fonts["large"], fill="#17221c")
                draw.text((372, 25), f"{today.day}日", font=fonts["body"], fill="#d13b2f")
                self._calendar(canvas, fonts, today)
                fitted = self._fit_photo(source, photo, (440, 420), crop_x, crop_y)
                canvas.paste(fitted, (20, 312))
                meta = "・".join(value for value in (caption, date_label, location) if value)
                draw.text((22, 754), self._fit_line(draw, meta, fonts["small"], width - 44), font=fonts["small"], fill="#354039")
                return canvas

            fitted = self._fit_photo(source, photo, (width, 505), crop_x, crop_y)
            canvas.paste(fitted, (0, 0))
            draw.line((20, 520, width - 20, 520), fill="#c9c1b2", width=2)
            if weather and weather.get("available"):
                outside = f"{weather_location}｜{weather['condition']}  {weather['temperature_c']:.0f}度"
                range_text = f"今日 {weather['minimum_c']:.0f}–{weather['maximum_c']:.0f}度  溼度 {weather['humidity_percent']:.0f}%"
            elif weather:
                outside = str(weather.get("condition", "天氣暫時無法取得"))
                range_text = "照片仍可正常顯示"
            else:
                outside = "天氣功能尚未啟用"
                range_text = "請至 Web 設定天氣位置"
            draw.text((22, 542), self._fit_line(draw, outside, fonts["large"], width - 44), font=fonts["large"], fill="#17221c")
            draw.text((24, 596), range_text, font=fonts["small"], fill="#4e5a52")
            if indoor:
                temperature = indoor.get("temperature_c")
                humidity = indoor.get("humidity_percent")
                values = []
                if temperature is not None:
                    values.append(f"{float(temperature):.1f}度")
                if humidity is not None:
                    values.append(f"{float(humidity):.0f}%")
                indoor_text = f"室內｜{indoor['device_name']}  " + "  ".join(values)
            else:
                indoor_text = "室內｜尚無 PhotoPainter 溫溼度回報"
            draw.text((24, 640), self._fit_line(draw, indoor_text, fonts["body"], width - 48), font=fonts["body"], fill="#1f4f70")
            meta = "・".join(value for value in (date_label, location, caption) if value)
            draw.text((24, 746), self._fit_line(draw, meta, fonts["small"], width - 48), font=fonts["small"], fill="#4e5a52")
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

    def _candidate_query(
        self,
        *,
        target: date,
        month_days: list[str] | None,
        older_only: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        memory_threshold = float(self.settings.get("render.memory_threshold", 70))
        with self.database.session() as connection:
            rows = connection.execute(
                    """
                    SELECT p.id,p.relative_path,p.captured_at,p.e6_score,p.e6_contrast_score,
                           p.e6_subject_score,p.e6_skin_score,p.e6_text_score,
                           p.crop_focus_x,p.crop_focus_y,p.crop_manual_x,p.crop_manual_y,
                           p.crop_method,p.crop_face_count,
                           COALESCE(a.ranking_score,a.memory_score) ranking_score,a.memory_score
                    FROM photos p
                    JOIN photo_analysis a ON a.id=(
                        SELECT id FROM photo_analysis WHERE photo_id=p.id ORDER BY created_at DESC LIMIT 1
                    )
                    WHERE p.status='analyzed' AND a.memory_score>=?
                      AND (?=0 OR substr(p.captured_at,6,5) IN (SELECT value FROM json_each(?)))
                      AND (?=0 OR (
                          p.captured_at IS NOT NULL
                          AND CAST(substr(p.captured_at,1,4) AS INTEGER) < ?
                      ))
                    ORDER BY COALESCE(a.ranking_score,a.memory_score) DESC LIMIT ?
                    """,
                    (
                        memory_threshold,
                        int(month_days is not None),
                        json.dumps(month_days or []),
                        int(older_only),
                        target.year,
                        max(limit, 1),
                    ),
                ).fetchall()
        weight = float(self.settings.get("render.e6_weight", 20)) / 100.0
        result = [dict(row) for row in rows]
        # 舊資料庫沒有構圖／E6 欄位值；只替最前面的候選照片做一次本機補算，
        # 避免為整個大型照片庫增加啟動延遲，也完全不會呼叫視覺模型。
        for row in result[: min(40, len(result))]:
            if row.get("crop_focus_x") is not None and row.get("e6_score") is not None:
                continue
            photo = self.photos.get_with_path(str(row["id"]))
            if photo is None:
                continue
            try:
                path = safe_join(Path(photo["root_path"]), photo["relative_path"])
                if not path.is_file():
                    continue
                refreshed = self._ensure_render_features(photo, path)
            except (OSError, ValueError):
                continue
            for key in (
                "e6_score",
                "e6_contrast_score",
                "e6_subject_score",
                "e6_skin_score",
                "e6_text_score",
                "crop_focus_x",
                "crop_focus_y",
                "crop_manual_x",
                "crop_manual_y",
                "crop_method",
                "crop_face_count",
            ):
                row[key] = refreshed[key]
        score_distribution = prepare_score_distribution(self.photos.score_population())
        for row in result:
            stored_ranking = row.get("ranking_score")
            ranking = float(stored_ranking) if isinstance(stored_ranking, (int, float, str)) else 0.0
            distinguishing, percentile = calculate_distinguishing_score(
                ranking, score_distribution
            )
            stored_e6 = row.get("e6_score")
            e6 = float(stored_e6) if isinstance(stored_e6, (int, float, str)) else 50.0
            row["raw_ranking_score"] = ranking
            row["ranking_percentile"] = percentile
            row["distinguishing_score"] = distinguishing
            row["combined_score"] = round(distinguishing * (1.0 - weight) + e6 * weight, 2)
        return sorted(result, key=lambda row: (-float(row["combined_score"]), str(row["id"])))

    def select_candidates_details(
        self, quantity: int | None = None, *, target_date: date | None = None
    ) -> list[dict[str, Any]]:
        limit = quantity if quantity is not None else int(self.settings.get("render.quantity", 5))
        limit = max(1, min(int(limit), 50))
        target = target_date or self._today()
        mode = str(self.settings.get("render.selection_mode", "history_today"))
        if mode == "top_ranked":
            rows = self._candidate_query(target=target, month_days=None, older_only=False, limit=500)
            for row in rows:
                row["match_type"] = "top_ranked"
                row["day_distance"] = None
            return rows[:limit]

        selected: list[dict[str, Any]] = []
        selected_ids: set[str] = set()

        def append(rows: list[dict[str, Any]], match_type: str, distances=None) -> None:
            for row in rows:
                photo_id = str(row["id"])
                if photo_id in selected_ids or len(selected) >= limit:
                    continue
                row["match_type"] = match_type
                row["day_distance"] = distances.get(str(row["captured_at"])[5:10]) if distances else 0
                selected.append(row)
                selected_ids.add(photo_id)

        month_day = target.strftime("%m-%d")
        exact = self._candidate_query(
            target=target, month_days=[month_day], older_only=True, limit=max(100, limit * 10)
        )
        append(exact, "exact_day")
        fallback = str(self.settings.get("render.history_today_fallback", "nearby_then_ranked"))
        window = int(self.settings.get("render.history_today_window_days", 7))
        if len(selected) < limit and window > 0 and fallback in {"nearby_then_ranked", "nearby_only"}:
            anchor = date(2000, target.month, target.day)
            distances: dict[str, int] = {}
            for offset in range(1, window + 1):
                distances[(anchor - timedelta(days=offset)).strftime("%m-%d")] = offset
                distances[(anchor + timedelta(days=offset)).strftime("%m-%d")] = offset
            nearby = self._candidate_query(
                target=target,
                month_days=list(distances),
                older_only=True,
                limit=max(300, limit * 30),
            )
            nearby.sort(key=lambda row: (distances.get(str(row["captured_at"])[5:10], 999), -float(row["combined_score"])))
            append(nearby, "nearby_day", distances)
        if len(selected) < limit and fallback in {"nearby_then_ranked", "ranked"}:
            ranked = self._candidate_query(target=target, month_days=None, older_only=False, limit=500)
            append(ranked, "ranked_fallback")
        return selected

    def select_candidates(self, quantity: int | None = None) -> list[str]:
        return [str(row["id"]) for row in self.select_candidates_details(quantity)]

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
