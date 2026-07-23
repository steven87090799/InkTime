# ruff: noqa: S608  # SQL fragments below are built only from server-controlled predicates.
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import random
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
from inktime.app.domain.rendering.adaptive_layout import (
    photo_orientation,
    select_pair_candidate,
)
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.repositories.render_candidates import RenderCandidateRepository
from inktime.app.repositories.settings import SettingsRepository
from inktime.app.services.weather import WeatherService
from inktime.app.services.release_coordinator import ReleaseCoordinator


LAYOUTS = {
    "full": "單張照片",
    "postcard": "明信片",
    "photo_info": "照片＋日期地點",
    "photo_pair": "雙照片拼版",
    "adaptive_memory": "智慧自適應回憶",
    "calendar": "月曆相框",
    "weather_sensor": "天氣＋室內溫溼度",
}
FRAME_ORIENTATIONS = {"portrait": "直向", "landscape": "橫向"}
FIT_MODES = {"contain": "完整顯示（建議）", "cover": "填滿並裁切"}
PORTRAIT_ONLY_LAYOUTS = {"calendar", "weather_sensor"}


class RenderService:
    def __init__(
        self,
        database: Database,
        photos: PhotoRepository,
        settings: SettingsRepository,
        fonts: FontManager,
        publisher: AtomicReleasePublisher,
        candidates: RenderCandidateRepository,
        release_coordinator: ReleaseCoordinator,
        locations: LocationResolver | None = None,
        weather: WeatherService | None = None,
        observability=None,
    ) -> None:
        self.database = database
        self.photos = photos
        self.settings = settings
        self.fonts = fonts
        self.publisher = publisher
        self.candidates = candidates
        self.release_coordinator = release_coordinator
        self.locations = locations
        self.weather = weather
        self.observability = observability

    def _activity(self, event: str, message: str, **fields) -> None:
        if self.observability is not None:
            self.observability.record("DEBUG", "renderer", event, message, **fields)

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
        fit_mode: str = "cover",
    ) -> Image.Image:
        if fit_mode == "contain":
            contained = ImageOps.contain(source, size, Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", size, "white")
            canvas.paste(
                contained,
                ((size[0] - contained.width) // 2, (size[1] - contained.height) // 2),
            )
            return canvas
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

    def _caption(self, photo_id: str) -> str:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT side_caption,semantic_json FROM photo_analysis WHERE photo_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (photo_id,),
            ).fetchone()
        if row is None:
            return ""
        side_caption = str(row["side_caption"] or "").strip()
        if not (bool(self.settings.get("analysis.advanced_caption_enabled", False)) and bool(self.settings.get("analysis.caption_variants_enabled", False))):
            return side_caption
        try:
            variants = (json.loads(str(row["semantic_json"] or "{}")).get("values") or {}).get("caption_variants") or {}
        except (TypeError, ValueError, json.JSONDecodeError):
            variants = {}
        style = str(self.settings.get("analysis.copy_default_style", "natural"))
        selected = str(variants.get(style) or variants.get("natural") or side_caption or "畫面把此刻收好了。").strip()
        self._activity("caption_style_selected", "Renderer 已選擇 Caption 候選風格", photo_id=photo_id, style=style)
        return selected

    def _draw_footer_caption(self, draw, text: str, *, x: int, top: int, bottom: int, width: int, fill: str = "black") -> None:
        font_path = self.fonts.resolve(str(self.settings.get("render.font_path", "")))
        body = ImageFont.truetype(str(font_path), 24)
        if not bool(self.settings.get("render.caption_wrap_enabled", False)):
            self._activity("caption_footer_single_line", "Footer Caption 維持單行截斷", wrap_enabled=False)
            draw.text((x, top), self._fit_line(draw, text, body, width), font=body, fill=fill)
            return
        maximum = min(2, int(self.settings.get("render.caption_max_lines", 2)))
        minimum = int(self.settings.get("render.caption_min_font_size", 17))
        for size in range(24, minimum - 1, -1):
            font = ImageFont.truetype(str(font_path), size)
            line_height = draw.textbbox((0, 0), "國", font=font)[3] + 2
            if line_height * maximum > bottom - top:
                continue
            lines: list[str] = []
            remaining = text.strip()
            while remaining and len(lines) < maximum:
                line = ""
                for char in remaining:
                    if draw.textlength(line + char, font=font) > width:
                        break
                    line += char
                if not line:
                    break
                lines.append(line)
                remaining = remaining[len(line):]
            if remaining and lines:
                lines[-1] = self._fit_line(draw, lines[-1] + remaining, font, width)
            if lines:
                self._activity("caption_footer_wrapped", "Footer Caption 已套用多行換行", wrap_enabled=True, lines=len(lines), font_size=size)
                draw.multiline_text((x, top), "\n".join(lines), font=font, fill=fill, spacing=2)
                return
        draw.text((x, top), self._fit_line(draw, text, body, width), font=body, fill=fill)

    def _adaptive_pair_candidates(self, primary: dict[str, Any]) -> list[dict[str, Any]]:
        """Use only existing analyzed/eligible rows; this is intentionally model-free."""
        with self.database.session() as connection:
            primary_analysis = connection.execute(
                "SELECT types_json,semantic_json FROM photo_analysis WHERE photo_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (str(primary["id"]),),
            ).fetchone()
            rows = connection.execute(
                f"""
                SELECT p.*,l.root_path,a.types_json,a.semantic_json,
                       EXISTS(SELECT 1 FROM display_history dh WHERE dh.photo_id=p.id) ever_displayed,
                       EXISTS(SELECT 1 FROM display_history dh WHERE dh.photo_id=p.id
                              AND dh.displayed_at>=datetime('now','-14 days')) recently_displayed
                FROM photos p JOIN libraries l ON l.id=p.library_id
                JOIN photo_analysis a ON a.id=(
                    SELECT latest.id FROM photo_analysis latest WHERE latest.photo_id=p.id
                    ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1
                )
                WHERE {RenderCandidateRepository.SQL_PREDICATE} AND p.id<>?
                ORDER BY CASE WHEN substr(p.captured_at,1,10)=substr(?,1,10) THEN 0 ELSE 1 END,
                         p.captured_at DESC,p.id DESC LIMIT 300
                """,
                (str(primary["id"]), str(primary.get("captured_at") or "")),
            ).fetchall()
        if primary_analysis is not None:
            try:
                semantic = json.loads(str(primary_analysis["semantic_json"] or "{}"))
                values = semantic.get("values", {}) if isinstance(semantic, dict) else {}
                primary["city"] = values.get("city_candidate")
                primary["types"] = json.loads(str(primary_analysis["types_json"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        candidates: list[dict[str, Any]] = []
        for stored in rows:
            row = dict(stored)
            if not self.candidates.available(row):
                continue
            try:
                semantic = json.loads(str(row.get("semantic_json") or "{}"))
                values = semantic.get("values", {}) if isinstance(semantic, dict) else {}
                row["city"] = values.get("city_candidate")
                row["types"] = json.loads(str(row.get("types_json") or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                row["city"], row["types"] = None, []
            candidates.append(row)
        return candidates

    def _adaptive_footer(
        self,
        draw: ImageDraw.ImageDraw,
        fonts: dict[str, ImageFont.FreeTypeFont],
        *,
        frame_width: int,
        frame_height: int,
        footer_height: int,
        primary,
        secondary=None,
        caption: str,
    ) -> None:
        photo_height = frame_height - footer_height
        draw.rectangle((0, photo_height, frame_width, frame_height), fill="white")
        draw.line((20, photo_height + 4, frame_width - 20, photo_height + 4), fill="black", width=2)
        text = caption or "這一天留下了兩個值得記住的片段。"
        self._draw_footer_caption(draw, text, x=22, top=photo_height + 12, bottom=frame_height - 38, width=frame_width - 44)
        primary_date = self._captured_date(primary["captured_at"])
        second_date = self._captured_date(secondary["captured_at"]) if secondary is not None else None
        dates = [self._date_label(primary_date)] if bool(self.settings.get("render.show_capture_date", True)) else []
        if second_date and second_date != primary_date:
            dates.append(self._date_label(second_date))
        first_location = self.location_name(primary)
        second_location = self.location_name(secondary) if secondary is not None else ""
        location = first_location if first_location == second_location else ""
        meta = "・".join(dates + ([location] if location else []))
        draw.text((22, frame_height - 32), self._fit_line(draw, meta, fonts["meta"], frame_width - 44), font=fonts["meta"], fill="black")

    @staticmethod
    def _physical_frame(canvas: Image.Image, orientation: str) -> Image.Image:
        """橫向先以 800×480 排版，再順時針旋轉成韌體固定的 480×800。"""
        if orientation == "landscape":
            return canvas.transpose(Image.Transpose.ROTATE_270)
        return canvas

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
        secondary_photo_id: str | None = None,
        orientation: str | None = None,
        fit_mode: str | None = None,
        device_config: dict[str, Any] | None = None,
    ) -> Image.Image:
        photo = self.ensure_photo_features(photo_id)
        path = safe_join(Path(photo["root_path"]), photo["relative_path"])
        caption = self._caption(photo_id)
        location = self.location_name(photo)
        captured = self._captured_date(photo["captured_at"])
        show_date = bool(self.settings.get("render.show_capture_date", True))
        date_label = self._date_label(captured) if show_date else ""
        device_config = device_config or {}
        layout_key = layout or str(device_config.get("layout_mode") or self.settings.get("render.layout", "photo_info"))
        if layout_key not in LAYOUTS:
            raise ValueError("RENDER-005 不支援的相框版型")
        adaptive_requested = layout_key == "adaptive_memory"
        orientation_key = orientation or str(device_config.get("frame_orientation") or self.settings.get("render.frame_orientation", "portrait"))
        if orientation_key not in FRAME_ORIENTATIONS:
            raise ValueError("RENDER-005 不支援的相框方向")
        fit_mode_key = fit_mode or str(device_config.get("fit_mode") or self.settings.get("render.fit_mode", "contain"))
        if fit_mode_key not in FIT_MODES:
            raise ValueError("RENDER-005 不支援的照片縮放方式")
        effective_orientation = (
            "portrait" if layout_key in PORTRAIT_ONLY_LAYOUTS else orientation_key
        )
        frame_width, frame_height = (
            (height, width) if effective_orientation == "landscape" else (width, height)
        )

        def finish(canvas: Image.Image) -> Image.Image:
            return self._physical_frame(canvas, effective_orientation)

        with Image.open(path) as opened:
            source = ImageOps.exif_transpose(opened).convert("RGB")
            if layout_key == "adaptive_memory":
                footer_height = 76 if effective_orientation == "landscape" else 96
                source_orientation = photo_orientation(source.size)
                if source_orientation in {"square", effective_orientation}:
                    layout_key = "photo_info"
                    fit_mode_key = "contain"
                else:
                    primary = dict(photo)
                    primary.update({"id": photo_id, "city": "", "types": []})
                    second_row = select_pair_candidate(
                        primary, self._adaptive_pair_candidates(primary), frame_orientation=effective_orientation
                    )
                    if second_row is None:
                        layout_key = "photo_info"
                        fit_mode_key = "contain"
                    else:
                        text_parts = [caption or "這一天留下了兩個值得記住的片段。", self.location_name(photo)]
                        fonts = self._fonts("\n".join(part for part in text_parts if part))
                        canvas = Image.new("RGB", (frame_width, frame_height), "white")
                        gutter = 8
                        if effective_orientation == "landscape":
                            slot_size = ((frame_width - gutter) // 2, frame_height - footer_height)
                            second_position = (slot_size[0] + gutter, 0)
                        else:
                            slot_size = (frame_width, (frame_height - footer_height - gutter) // 2)
                            second_position = (0, slot_size[1] + gutter)
                        canvas.paste(self._fit_photo(source, photo, slot_size, None, None, "contain"), (0, 0))
                        second_path = safe_join(Path(second_row["root_path"]), second_row["relative_path"])
                        with Image.open(second_path) as second_opened:
                            second_source = ImageOps.exif_transpose(second_opened).convert("RGB")
                            canvas.paste(self._fit_photo(second_source, second_row, slot_size, None, None, "contain"), second_position)
                        self._adaptive_footer(
                            ImageDraw.Draw(canvas), fonts, frame_width=frame_width, frame_height=frame_height,
                            footer_height=footer_height, primary=photo, secondary=second_row, caption=caption,
                        )
                        return finish(canvas)
            if layout_key == "full":
                return finish(
                    self._fit_photo(
                        source,
                        photo,
                        (frame_width, frame_height),
                        crop_x,
                        crop_y,
                        fit_mode_key,
                    )
                )

            today = self._today()
            weather = self.weather.current() if self.weather and layout_key == "weather_sensor" else None
            indoor = self._latest_indoor() if layout_key == "weather_sensor" else None
            weather_location = str(self.settings.get("render.weather_location_name", "所在地"))
            text_parts = [caption, location, date_label, f"{today.month}月{today.day}日", "星期一二三四五六日"]
            if adaptive_requested:
                text_parts.extend(["這一天留下了一個值得記住的片段。", "這一天留下了兩個值得記住的片段。"])
            if layout_key == "photo_pair":
                text_parts.append("請選擇第二張照片")
            if weather:
                text_parts.extend([str(weather.get("condition", "")), weather_location, "室外室內最高最低溫溼度"])
            if indoor:
                text_parts.extend([str(indoor.get("device_name", "")), "室內溫度濕度"])
            fonts = self._fonts("\n".join(part for part in text_parts if part))
            # 資訊區使用真正的面板白色，避免米白經抖動後變成彩色雜點。
            canvas = Image.new("RGB", (frame_width, frame_height), "white")
            draw = ImageDraw.Draw(canvas)

            if layout_key == "photo_pair":
                gutter = 8
                if effective_orientation == "landscape":
                    first_size = ((frame_width - gutter) // 2, frame_height)
                    second_position = (first_size[0] + gutter, 0)
                else:
                    first_size = (frame_width, (frame_height - gutter) // 2)
                    second_position = (0, first_size[1] + gutter)
                first = self._fit_photo(
                    source, photo, first_size, crop_x, crop_y, fit_mode_key
                )
                canvas.paste(first, (0, 0))
                if secondary_photo_id:
                    second_photo = self.ensure_photo_features(secondary_photo_id)
                    second_path = safe_join(
                        Path(second_photo["root_path"]), second_photo["relative_path"]
                    )
                    with Image.open(second_path) as second_opened:
                        second_source = ImageOps.exif_transpose(second_opened).convert("RGB")
                        second = self._fit_photo(
                            second_source,
                            second_photo,
                            first_size,
                            None,
                            None,
                            fit_mode_key,
                        )
                    canvas.paste(second, second_position)
                else:
                    placeholder = "請選擇第二張照片"
                    text_width = draw.textlength(placeholder, font=fonts["body"])
                    draw.text(
                        (
                            second_position[0] + max(18, (first_size[0] - text_width) / 2),
                            second_position[1] + first_size[1] / 2 - 14,
                        ),
                        placeholder,
                        font=fonts["body"],
                        fill="black",
                    )
                return finish(canvas)

            if layout_key == "postcard":
                footer_height = 122 if effective_orientation == "landscape" else 142
                photo_size = (frame_width - 48, frame_height - footer_height - 24)
                fitted = self._fit_photo(
                    source, photo, photo_size, crop_x, crop_y, fit_mode_key
                )
                canvas.paste(fitted, (24, 24))
                draw.rectangle(
                    (23, 23, frame_width - 24, frame_height - footer_height + 1),
                    outline="#b9afa0",
                    width=2,
                )
                if caption:
                    self._draw_footer_caption(draw, caption, x=28, top=frame_height - footer_height + 16, bottom=frame_height - 48, width=frame_width - 56, fill="#1b241f")
                meta = "・".join(value for value in (date_label, location) if value)
                draw.text(
                    (28, frame_height - 42),
                    self._fit_line(draw, meta, fonts["small"], frame_width - 56),
                    font=fonts["small"],
                    fill="#59605a",
                )
                return finish(canvas)

            if layout_key == "photo_info":
                info_height = 76 if effective_orientation == "landscape" else 96
                photo_height = frame_height - info_height
                fitted = self._fit_photo(
                    source,
                    photo,
                    (frame_width, photo_height),
                    crop_x,
                    crop_y,
                    fit_mode_key,
                )
                canvas.paste(fitted, (0, 0))
                draw.rectangle((0, photo_height, frame_width, frame_height), fill="white")
                draw.line(
                    (20, photo_height + 4, frame_width - 20, photo_height + 4),
                    fill="black",
                    width=2,
                )
                footer_caption = caption or (
                    "這一天留下了一個值得記住的片段。" if adaptive_requested else ""
                )
                if footer_caption:
                    self._draw_footer_caption(draw, footer_caption, x=22, top=photo_height + 12, bottom=frame_height - 38, width=frame_width - 44)
                meta = "・".join(value for value in (date_label, location) if value)
                draw.text(
                    (22, frame_height - 32),
                    self._fit_line(draw, meta, fonts["meta"], frame_width - 44),
                    font=fonts["meta"],
                    fill="black",
                )
                return finish(canvas)

            if layout_key == "calendar":
                draw.text((24, 16), f"{today.year}年 {today.month}月", font=fonts["large"], fill="#17221c")
                draw.text((372, 25), f"{today.day}日", font=fonts["body"], fill="#d13b2f")
                self._calendar(canvas, fonts, today)
                fitted = self._fit_photo(
                    source, photo, (440, 420), crop_x, crop_y, fit_mode_key
                )
                canvas.paste(fitted, (20, 312))
                meta = "・".join(value for value in (caption, date_label, location) if value)
                draw.text((22, 754), self._fit_line(draw, meta, fonts["small"], width - 44), font=fonts["small"], fill="#354039")
                return finish(canvas)

            fitted = self._fit_photo(
                source, photo, (width, 505), crop_x, crop_y, fit_mode_key
            )
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
            return finish(canvas)

    def publish(
        self,
        photo_ids: list[str],
        created_by: str,
        profile_keys: list[str] | None = None,
        history: dict[str, str] | None = None,
        device_ids: list[str] | None = None,
    ) -> dict:
        quantity = int(self.settings.get("render.quantity", 5))
        layout_key = str(self.settings.get("render.layout", "photo_info"))
        source_limit = quantity * 2 if layout_key == "photo_pair" else quantity
        selected = photo_ids[:source_limit]
        if not selected:
            selected = self.select_candidates(source_limit)
        else:
            # 明確指定不合格照片必須穩定失敗；不得靜默改選其他照片。
            selected = [str(row["id"]) for row in self.candidates.require(selected)]
        if device_ids:
            unique_device_ids = list(dict.fromkeys(str(value) for value in device_ids if str(value)))
            placeholders = ",".join("?" for _ in unique_device_ids)
            with self.database.session() as connection:
                devices = connection.execute(
                    f"SELECT * FROM devices WHERE enabled=1 AND id IN ({placeholders})",  # noqa: S608
                    unique_device_ids,
                ).fetchall()
            by_id = {str(row["id"]): dict(row) for row in devices}
            missing = [device_id for device_id in unique_device_ids if device_id not in by_id]
            if missing:
                raise ValueError("DISPLAY-004 指定裝置不存在或已停用")
            manifests = []
            assignments: dict[str, str] = {}
            for device_id in unique_device_ids:
                device = by_id[device_id]
                profile_key = str(device["panel_profile"])
                if profile_key not in DISPLAY_PROFILES:
                    raise ValueError("RENDER-003 發布包含不支援的顯示 Profile")
                images = [
                    (
                        photo_id,
                        self.render_photo(photo_id, device_config=device),
                    )
                    for photo_id in selected[:quantity]
                ]
                manifest = self.publisher.publish(
                    images,
                    profile_key=profile_key,
                    dither=str(self.settings.get("render.dither", "floyd_steinberg")),
                    color_distance=str(self.settings.get("render.color_distance", "oklab")),
                    dither_strength=float(self.settings.get("render.dither_strength", 1.0)),
                    orientation=str(device.get("frame_orientation") or self.settings.get("render.frame_orientation", "portrait")),
                    activate=False,
                    metadata={"device_id": device_id, "layout_mode": device.get("layout_mode") or layout_key},
                )
                manifests.append(manifest)
                assignments[device_id] = str(manifest["release_id"])
            published = self.release_coordinator.publish(
                manifests, created_by=created_by, photo_ids=selected[:quantity], history=history,
                device_assignments=assignments,
            )
            return {"releases": published, "device_releases": assignments}
        if layout_key == "photo_pair":
            images = []
            for index in range(0, len(selected), 2):
                primary_id = selected[index]
                secondary_id = selected[index + 1] if index + 1 < len(selected) else None
                if secondary_id is None:
                    images.append(
                        (primary_id, self.render_photo(primary_id, layout="photo_info"))
                    )
                else:
                    images.append(
                        (
                            f"{primary_id}+{secondary_id}",
                            self.render_photo(
                                primary_id,
                                layout="photo_pair",
                                secondary_photo_id=secondary_id,
                            ),
                        )
                    )
        else:
            images = [(photo_id, self.render_photo(photo_id)) for photo_id in selected]
        selected_profiles = profile_keys or [str(self.settings.get("render.profile", "safe_4c"))]
        selected_profiles = list(dict.fromkeys(selected_profiles))
        if not selected_profiles or any(key not in DISPLAY_PROFILES for key in selected_profiles):
            raise ValueError("RENDER-003 發布包含不支援的顯示 Profile")
        dither = str(self.settings.get("render.dither", "floyd_steinberg"))
        color_distance = str(self.settings.get("render.color_distance", "oklab"))
        dither_strength = float(self.settings.get("render.dither_strength", 1.0))
        requested_orientation = str(
            self.settings.get("render.frame_orientation", "portrait")
        )
        release_orientation = (
            "portrait"
            if layout_key in PORTRAIT_ONLY_LAYOUTS
            else requested_orientation
        )
        manifests = []
        for profile_key in selected_profiles:
            manifest = self.publisher.publish(
                images,
                profile_key=profile_key,
                dither=dither,
                color_distance=color_distance,
                dither_strength=dither_strength,
                orientation=release_orientation,
                activate=False,
            )
            manifests.append(manifest)
        published = self.release_coordinator.publish(
            manifests,
            created_by=created_by,
            photo_ids=selected,
            history=history,
        )
        return published[0] if len(published) == 1 else {"releases": published}

    def _candidate_query(
        self,
        *,
        target: date,
        month_days: list[str] | None,
        older_only: bool,
        limit: int,
        candidate_years: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        memory_threshold = float(self.settings.get("render.memory_threshold", 70))
        weight = float(self.settings.get("render.e6_weight", 20)) / 100.0
        result: list[dict[str, Any]] = []
        offset = 0
        # 檔案存在性無法安全地交給 SQLite；用固定批次掃描 SQL 已排序結果，
        # 每次只保留真正可用的候選，避免把大型照片庫 materialize 成 Dict。
        while len(result) < max(limit, 1):
            with self.database.session() as connection:
                rows = connection.execute(
                    f"""
                    SELECT p.id,p.relative_path,p.captured_at,p.e6_score,p.e6_contrast_score,
                           p.e6_subject_score,p.e6_skin_score,p.e6_text_score,
                           p.crop_focus_x,p.crop_focus_y,p.crop_manual_x,p.crop_manual_y,
                           p.crop_method,p.crop_face_count,l.root_path,
                           COALESCE(a.final_ranking_score,a.ranking_score,a.memory_score,0) ranking_score,
                           a.memory_score,
                           (COALESCE(a.final_ranking_score,a.ranking_score,a.memory_score,0) * ?
                            + COALESCE(p.e6_score,50) * ?) combined_score
                    FROM photos p
                    JOIN libraries l ON l.id=p.library_id
                    JOIN photo_analysis a ON a.id=(
                        SELECT latest.id FROM photo_analysis latest WHERE latest.photo_id=p.id
                        ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1
                    )
                    WHERE {RenderCandidateRepository.SQL_PREDICATE}
                      AND a.memory_score>=?
                      AND (?=0 OR substr(p.captured_at,6,5) IN (SELECT value FROM json_each(?)))
                      AND (?=0 OR (
                          p.captured_at IS NOT NULL
                          AND CAST(substr(p.captured_at,1,4) AS INTEGER) < ?
                      ))
                      AND (?=0 OR CAST(substr(p.captured_at,1,4) AS INTEGER)
                          IN (SELECT value FROM json_each(?)))
                    ORDER BY combined_score DESC,p.id LIMIT 250 OFFSET ?
                    """,  # noqa: S608 -- eligibility predicate is a fixed class constant
                    (
                        1.0 - weight,
                        weight,
                        memory_threshold,
                        int(month_days is not None),
                        json.dumps(month_days or []),
                        int(older_only),
                        target.year,
                        int(bool(candidate_years)),
                        json.dumps(candidate_years or []),
                        offset,
                    ),
                ).fetchall()
            if not rows:
                break
            for stored in rows:
                if self.candidates.available(stored):
                    result.append(dict(stored))
                    if len(result) >= max(limit, 1):
                        break
            offset += len(rows)
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
        for row in result:
            stored_ranking = row.get("ranking_score")
            ranking = float(stored_ranking) if isinstance(stored_ranking, (int, float, str)) else 0.0
            row["raw_ranking_score"] = ranking
            row["ranking_percentile"] = None
            row["distinguishing_score"] = ranking
            row["combined_score"] = round(float(row["combined_score"]), 2)
        return result

    def select_candidates_details(
        self,
        quantity: int | None = None,
        *,
        target_date: date | None = None,
        candidate_years: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        limit = quantity if quantity is not None else int(self.settings.get("render.quantity", 5))
        limit = max(1, min(int(limit), 50))
        target = target_date or self._today()
        mode = str(self.settings.get("render.selection_mode", "history_today"))
        if mode == "top_ranked":
            rows = self._candidate_query(target=target, month_days=None, older_only=False, limit=500, candidate_years=candidate_years)
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
            target=target, month_days=[month_day], older_only=True, limit=max(100, limit * 10), candidate_years=candidate_years
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
                candidate_years=candidate_years,
            )
            nearby.sort(key=lambda row: (distances.get(str(row["captured_at"])[5:10], 999), -float(row["combined_score"])))
            append(nearby, "nearby_day", distances)
        if len(selected) < limit and fallback in {"nearby_then_ranked", "ranked"}:
            ranked = self._candidate_query(target=target, month_days=None, older_only=False, limit=500, candidate_years=candidate_years)
            append(ranked, "ranked_fallback")
        return selected

    def select_candidates(self, quantity: int | None = None) -> list[str]:
        return [str(row["id"]) for row in self.select_candidates_details(quantity)]

    @staticmethod
    def _history_type_filter(value: str) -> str | None:
        return {
            "person": "人物",
            "travel": "旅行",
            "landscape": "風景",
        }.get(value)

    def _history_where(self, filters: dict[str, Any], *, month_day: str | None = None) -> tuple[str, list[Any]]:
        clauses = [
            RenderCandidateRepository.SQL_PREDICATE,
            "p.captured_at IS NOT NULL",
        ]
        params: list[Any] = []
        start_year = filters.get("start_year")
        end_year = filters.get("end_year")
        if isinstance(start_year, int):
            clauses.append("CAST(substr(p.captured_at,1,4) AS INTEGER)>=?")
            params.append(start_year)
        if isinstance(end_year, int):
            clauses.append("CAST(substr(p.captured_at,1,4) AS INTEGER)<=?")
            params.append(end_year)
        if month_day:
            clauses.append("substr(p.captured_at,6,5)=?")
            params.append(month_day)
        type_name = self._history_type_filter(str(filters.get("type", "")))
        if type_name:
            clauses.append("EXISTS (SELECT 1 FROM json_each(COALESCE(a.types_json,'[]')) WHERE value=?)")
            params.append(type_name)
        for key, json_path in (("city", "$.values.city_candidate"), ("country", "$.values.country_candidate")):
            value = str(filters.get(key, "")).strip()
            if value:
                clauses.append("lower(COALESCE(json_extract(a.semantic_json, ?),''))=lower(?)")
                params.extend((json_path, value))
        recent_days = filters.get("exclude_recent_days")
        if isinstance(recent_days, int) and recent_days > 0:
            clauses.append("NOT EXISTS (SELECT 1 FROM display_history dh WHERE dh.photo_id=p.id AND dh.displayed_at>=datetime('now', ?))")
            params.append(f"-{recent_days} days")
        if bool(filters.get("unseen_only")):
            clauses.append("NOT EXISTS (SELECT 1 FROM display_history dh WHERE dh.photo_id=p.id)")
        return " AND ".join(clauses), params

    def _history_rows(
        self,
        filters: dict[str, Any],
        *,
        month_day: str | None = None,
        history_date: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str = "p.captured_at,p.id",
    ) -> list[dict[str, Any]]:
        """Fetch a bounded, indexed candidate set; never decode image contents here."""
        where, params = self._history_where(filters, month_day=month_day)
        if history_date:
            where += " AND substr(p.captured_at,1,10)=?"
            params.append(history_date)
        allowed_orders = {
            "p.captured_at,p.id",
            "final_score DESC,p.id",
        }
        if order_by not in allowed_orders:
            raise ValueError("HISTORY-001 候選排序不合法")
        with self.database.session() as connection:
            rows = connection.execute(
                f"""
                SELECT p.id,p.relative_path,p.captured_at,p.local_candidate_score,p.exclusion_status,
                       p.manual_override,l.root_path,p.e6_score,
                       a.provider,a.model,a.prompt_version,a.schema_version,a.ranking_rule_version,
                       a.memory_score,a.beauty_score,a.technical_quality_score,a.final_ranking_score,
                       a.ranking_score,a.travel_bonus,a.location_rule_version,a.types_json,a.semantic_json,
                       COALESCE(a.final_ranking_score,a.ranking_score,a.memory_score,p.local_candidate_score,0) AS final_score
                FROM photos p
                JOIN libraries l ON l.id=p.library_id
                JOIN photo_analysis a ON a.id=(
                    SELECT latest.id FROM photo_analysis latest
                    WHERE latest.photo_id=p.id ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1
                )
                WHERE {where}
                ORDER BY {order_by}
                LIMIT ? OFFSET ?
                """,
                (*params, max(1, min(limit, 500)), max(0, offset)),
            ).fetchall()
        usable: list[dict[str, Any]] = []
        for stored in rows:
            row = dict(stored)
            available = self.candidates.available(row)
            if available:
                row["available"] = True
                try:
                    details = json.loads(str(row.get("semantic_json") or "{}"))
                except json.JSONDecodeError:
                    details = {}
                values = details.get("values", {}) if isinstance(details, dict) else {}
                row["city"] = values.get("city_candidate")
                row["country"] = values.get("country_candidate")
                row["types"] = json.loads(str(row.get("types_json") or "[]"))
                usable.append(row)
        return usable

    def _iter_history_rows(
        self,
        filters: dict[str, Any],
        *,
        month_day: str | None = None,
        history_date: str | None = None,
        order_by: str = "p.captured_at,p.id",
    ):
        offset = 0
        while True:
            batch = self._history_rows(
                filters,
                month_day=month_day,
                history_date=history_date,
                limit=500,
                offset=offset,
                order_by=order_by,
            )
            # Offset 必須依 DB batch 前進；若可用列少於 500，可能只是檔案缺失。
            with self.database.session() as connection:
                where, params = self._history_where(filters, month_day=month_day)
                if history_date:
                    where += " AND substr(p.captured_at,1,10)=?"
                    params.append(history_date)
                raw_order = (
                    "COALESCE(a.final_ranking_score,a.ranking_score,a.memory_score,p.local_candidate_score,0) DESC,p.id"
                    if order_by == "final_score DESC,p.id"
                    else order_by
                )
                raw_batch = connection.execute(
                        f"SELECT p.id FROM photos p JOIN libraries l ON l.id=p.library_id "
                        f"JOIN photo_analysis a ON a.id=(SELECT latest.id FROM photo_analysis latest WHERE latest.photo_id=p.id ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1) "
                        f"WHERE {where} ORDER BY {raw_order} LIMIT 500 OFFSET ?",  # noqa: S608 -- fixed predicates and validated order
                        (*params, offset),
                    ).fetchall()
            yield from batch
            if len(raw_batch) < 500:
                break
            offset += 500

    def _history_dates(self, filters: dict[str, Any]) -> list[str]:
        """Return dates only, so a 100,000-row library is never materialized for a random pick."""
        where, params = self._history_where(filters)
        analysis_join = (
            "JOIN libraries l ON l.id=p.library_id "
            "JOIN photo_analysis a ON a.id=(SELECT latest.id FROM photo_analysis latest "
            "WHERE latest.photo_id=p.id ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)"
        )
        with self.database.session() as connection:
            rows = connection.execute(
                f"SELECT DISTINCT substr(p.captured_at,1,10) AS history_date FROM photos p "  # noqa: S608 - clauses are fixed local SQL fragments
                f"{analysis_join} "
                f"WHERE {where} ORDER BY history_date",
                params,
            ).fetchall()
        return [str(row["history_date"]) for row in rows]

    @staticmethod
    def _validated_history_filters(payload: dict[str, Any]) -> dict[str, Any]:
        filters: dict[str, Any] = {}
        for key in ("start_year", "end_year", "exclude_recent_days"):
            value = payload.get(key)
            if value is None or value == "":
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"HISTORY-001 {key} 必須是整數") from exc
            if key == "exclude_recent_days":
                if not 0 <= parsed <= 3650:
                    raise ValueError("HISTORY-001 排除近期天數必須介於 0 到 3650")
            elif not 1900 <= parsed <= 2200:
                raise ValueError("HISTORY-001 年份必須介於 1900 到 2200")
            filters[key] = parsed
        if filters.get("start_year", 1900) > filters.get("end_year", 2200):
            raise ValueError("HISTORY-001 起始年份不得晚於結束年份")
        type_name = str(payload.get("type", "")).strip()
        if type_name and type_name not in {"person", "travel", "landscape"}:
            raise ValueError("HISTORY-001 照片類型不合法")
        filters["type"] = type_name
        filters["city"] = str(payload.get("city", "")).strip()[:80]
        filters["country"] = str(payload.get("country", "")).strip()[:80]
        filters["unseen_only"] = bool(payload.get("unseen_only", False))
        return filters

    def select_random_history_day(self, payload: dict[str, Any], *, rng: random.Random | None = None) -> dict[str, Any]:
        filters = self._validated_history_filters(payload)
        dates = self._history_dates(filters)
        if not dates:
            return {"status": "empty", "message": "找不到符合所有篩選條件且目前檔案可用的歷史照片；未放寬任何條件。", "filters": filters}
        picker = rng or random.SystemRandom()
        remaining = list(dates)
        while remaining:
            chosen_date = picker.choice(remaining)
            candidates = list(
                self._iter_history_rows(
                    filters, month_day=chosen_date[5:10], history_date=chosen_date
                )
            )
            if candidates:
                candidates.sort(key=lambda row: (-float(row["final_score"]), str(row["id"])))
                return self._history_selection(chosen_date, candidates, "random_history_day", filters)
            remaining.remove(chosen_date)
        return {"status": "empty", "message": "找不到符合所有篩選條件且目前檔案可用的歷史照片；未放寬任何條件。", "filters": filters}

    def reroll_history_day(self, payload: dict[str, Any], *, rng: random.Random | None = None) -> dict[str, Any]:
        month_day = str(payload.get("month_day", "")).strip()
        try:
            datetime.strptime(month_day, "%m-%d")
        except ValueError as exc:
            raise ValueError("HISTORY-001 month_day 必須是 MM-DD") from exc
        filters = self._validated_history_filters(payload)
        current_id = str(payload.get("current_photo_id", "")).strip()
        rows = (
            row
            for row in self._iter_history_rows(
                filters,
                month_day=month_day,
                order_by="final_score DESC,p.id" if str(payload.get("mode")) == "top_n" else "p.captured_at,p.id",
            )
            if str(row["id"]) != current_id
        )
        mode = str(payload.get("mode", "random"))
        if mode not in {"random", "weighted", "top_n", "prefer_unseen", "prefer_travel", "prefer_person"}:
            raise ValueError("HISTORY-001 同日重抽模式不合法")
        picker = rng or random.SystemRandom()
        selected = None
        seen = 0
        preferred_seen = 0
        weighted_total = 0.0
        top_limit = max(1, min(int(payload.get("top_n", 10)), 100))
        fallback = None

        def reservoir(current, candidate, count: int):
            return candidate if current is None or picker.choice(range(count)) == 0 else current

        for row in rows:
            seen += 1
            fallback = reservoir(fallback, row, seen)
            if mode == "top_n":
                if seen > top_limit:
                    break
                selected = reservoir(selected, row, seen)
            elif mode == "weighted":
                weight = max(0.1, float(row["final_score"]))
                weighted_total += weight
                random_value = getattr(picker, "random", random.SystemRandom().random)()
                if selected is None or random_value < weight / weighted_total:
                    selected = row
            elif mode in {"prefer_travel", "prefer_person"}:
                wanted = "旅行" if mode == "prefer_travel" else "人物"
                if wanted in row.get("types", []):
                    preferred_seen += 1
                    selected = reservoir(selected, row, preferred_seen)
            elif mode == "prefer_unseen":
                if not self._was_displayed(str(row["id"])):
                    preferred_seen += 1
                    selected = reservoir(selected, row, preferred_seen)
            else:
                selected = reservoir(selected, row, seen)
        selected = selected or fallback
        if selected is None:
            return {"status": "empty", "message": "此月日沒有其他符合條件的可用照片，沒有重試或改選其他日期。", "filters": filters, "month_day": month_day}
        return self._history_selection(str(selected["captured_at"])[:10], [selected], f"same_day_{mode}", filters)

    def _was_displayed(self, photo_id: str) -> bool:
        with self.database.session() as connection:
            return bool(
                connection.execute(
                    "SELECT 1 FROM display_history WHERE photo_id=? LIMIT 1", (photo_id,)
                ).fetchone()
            )

    def _history_selection(self, history_date: str, candidates: list[dict[str, Any]], method: str, filters: dict[str, Any]) -> dict[str, Any]:
        for candidate in candidates:
            candidate["candidate_count"] = len(candidates)
            candidate["selection_method"] = method
            candidate["history_date"] = history_date
            candidate["month_day"] = history_date[5:10]
            candidate["final_score"] = round(float(candidate["final_score"]), 2)
        return {"status": "ok", "history_date": history_date, "month_day": history_date[5:10], "candidate_count": len(candidates), "selection_method": method, "filters": filters, "candidates": candidates}

    def record_display(self, photo_ids: list[str], *, selection_method: str, history_date: str, release_id: str | None = None) -> None:
        if not photo_ids:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.executemany(
                "INSERT INTO display_history(photo_id,history_date,selection_method,release_id,displayed_at,metadata_json) VALUES (?,?,?,?,?,?)",
                [(photo_id, history_date, selection_method, release_id, now, "{}") for photo_id in photo_ids],
            )

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
