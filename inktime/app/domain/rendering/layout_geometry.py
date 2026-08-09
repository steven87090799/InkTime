"""Shared logical frame geometry for formal rendering and Web previews.

The renderer composes in a logical frame (800x480 for landscape and
480x800 for portrait) and rotates landscape output only at the physical
device boundary.  Keeping the rectangles here makes the preview able to
scale the same slots without inventing a second set of percentages in CSS.
"""

from __future__ import annotations

from dataclasses import dataclass


PORTRAIT_ONLY_LAYOUTS = frozenset({"calendar", "weather_sensor"})
SUPPORTED_LAYOUTS = frozenset(
    {
        "full",
        "photo_info",
        "postcard",
        "photo_pair",
        "photo_pair_caption",
        "adaptive_memory",
        "calendar",
        "weather_sensor",
    }
)


@dataclass(frozen=True)
class Rect:
    """An integer rectangle in logical frame coordinates."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def as_dict(self) -> dict[str, int]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class LayoutGeometry:
    """Normalized rectangles used by a layout.

    ``primary_photo``/``secondary_photo`` are the image slots.  Caption
    rectangles are reserved bands for the corresponding photo.  ``info``
    contains non-pair information bands such as the photo-info footer.
    ``canvas_width`` and ``canvas_height`` are always normalized to the
    requested effective orientation.
    """

    layout: str
    orientation: str
    canvas_width: int
    canvas_height: int
    primary_photo: Rect | None = None
    secondary_photo: Rect | None = None
    primary_caption: Rect | None = None
    secondary_caption: Rect | None = None
    info: tuple[Rect, ...] = ()
    gutter: int = 0

    @property
    def photo_rect(self) -> Rect | None:
        return self.primary_photo

    @property
    def secondary_photo_rect(self) -> Rect | None:
        return self.secondary_photo

    @property
    def caption_rect(self) -> Rect | None:
        return self.primary_caption

    @property
    def secondary_caption_rect(self) -> Rect | None:
        return self.secondary_caption

    @property
    def info_rect(self) -> Rect | None:
        return self.info[0] if self.info else None

    @property
    def photo_rects(self) -> tuple[Rect, ...]:
        return tuple(rect for rect in (self.primary_photo, self.secondary_photo) if rect is not None)

    @property
    def caption_rects(self) -> tuple[Rect, ...]:
        return tuple(rect for rect in (self.primary_caption, self.secondary_caption) if rect is not None)

    @property
    def info_rects(self) -> tuple[Rect, ...]:
        return self.info

    def as_dict(self) -> dict[str, object]:
        return {
            "layout": self.layout,
            "orientation": self.orientation,
            "canvas_width": self.canvas_width,
            "canvas_height": self.canvas_height,
            "primary_photo": self.primary_photo.as_dict() if self.primary_photo else None,
            "secondary_photo": self.secondary_photo.as_dict() if self.secondary_photo else None,
            "primary_caption": self.primary_caption.as_dict() if self.primary_caption else None,
            "secondary_caption": self.secondary_caption.as_dict() if self.secondary_caption else None,
            "info": [rect.as_dict() for rect in self.info],
            "info_rect": self.info[0].as_dict() if self.info else None,
            "photo_rects": [rect.as_dict() for rect in self.photo_rects],
            "caption_rects": [rect.as_dict() for rect in self.caption_rects],
            "info_rects": [rect.as_dict() for rect in self.info],
            "gutter": self.gutter,
        }


def _normalized_canvas(orientation: str, width: int, height: int) -> tuple[int, int]:
    if orientation not in {"portrait", "landscape"}:
        raise ValueError("orientation must be portrait or landscape")
    if sorted((width, height)) != [480, 800]:
        raise ValueError("canvas dimensions must be 480x800 or 800x480")
    if orientation == "portrait" and width > height:
        return height, width
    if orientation == "landscape" and height > width:
        return height, width
    return width, height


def _single_info_geometry(
    *, layout: str, orientation: str, width: int, height: int, caption_ratio: float
) -> LayoutGeometry:
    if layout == "full":
        return LayoutGeometry(layout, orientation, width, height, primary_photo=Rect(0, 0, width, height))
    if layout == "photo_info":
        info_height = 76 if orientation == "landscape" else 96
        photo = Rect(0, 0, width, height - info_height)
        info = Rect(0, photo.bottom, width, info_height)
        return LayoutGeometry(layout, orientation, width, height, photo, info=(info,))
    if layout == "postcard":
        footer_height = 122 if orientation == "landscape" else 142
        photo = Rect(24, 24, width - 48, height - footer_height - 24)
        info = Rect(24, height - footer_height, width - 48, footer_height)
        return LayoutGeometry(layout, orientation, width, height, photo, info=(info,))
    if layout == "calendar":
        photo = Rect(20, 312, 440, 420)
        caption = Rect(22, 754, max(1, width - 44), max(1, height - 754))
        return LayoutGeometry(layout, orientation, width, height, photo, primary_caption=caption, info=(Rect(0, 0, width, 312),))
    if layout == "weather_sensor":
        photo = Rect(0, 0, width, 505)
        caption = Rect(24, 746, max(1, width - 48), max(1, height - 746))
        return LayoutGeometry(layout, orientation, width, height, photo, primary_caption=caption, info=(Rect(0, 505, width, height - 505),))
    if layout == "adaptive_memory":
        # Without source aspect metadata the deterministic preview represents
        # the single-photo fallback.  RenderService uses the same footer
        # dimensions when adaptive selection resolves to one photo.
        info_height = 76 if orientation == "landscape" else 96
        photo = Rect(0, 0, width, height - info_height)
        info = Rect(0, photo.bottom, width, info_height)
        return LayoutGeometry(layout, orientation, width, height, photo, info=(info,))
    raise ValueError(f"unsupported single layout: {layout}")


def resolve_layout_geometry(
    layout: str,
    orientation: str,
    canvas_width: int,
    canvas_height: int,
    caption_ratio: float = 0.20,
) -> LayoutGeometry:
    """Resolve all layout slots in one normalized logical coordinate system.

    The supplied dimensions must be the canonical physical 480x800 canvas in
    either order.  They are swapped when necessary so landscape is wide and
    portrait is tall.  Rejecting other sizes keeps the historical fixed-pixel
    calendar and weather contracts in bounds; browser previews scale the
    canonical rectangles proportionally.
    """

    if layout not in SUPPORTED_LAYOUTS:
        raise ValueError(f"unsupported layout: {layout}")
    effective_orientation = "portrait" if layout in PORTRAIT_ONLY_LAYOUTS else orientation
    width, height = _normalized_canvas(effective_orientation, int(canvas_width), int(canvas_height))
    ratio = float(caption_ratio)
    if not 0 < ratio < 1:
        ratio = 0.20

    if layout not in {"photo_pair", "photo_pair_caption"}:
        return _single_info_geometry(
            layout=layout,
            orientation=effective_orientation,
            width=width,
            height=height,
            caption_ratio=ratio,
        )

    gutter = 8
    if layout == "photo_pair":
        if effective_orientation == "landscape":
            primary_width = (width - gutter) // 2
            primary = Rect(0, 0, primary_width, height)
            secondary = Rect(primary_width + gutter, 0, width - primary_width - gutter, height)
        else:
            primary_height = (height - gutter) // 2
            primary = Rect(0, 0, width, primary_height)
            secondary = Rect(0, primary_height + gutter, width, height - primary_height - gutter)
        return LayoutGeometry(
            layout,
            effective_orientation,
            width,
            height,
            primary_photo=primary,
            secondary_photo=secondary,
            gutter=gutter,
        )

    if effective_orientation == "landscape":
        card_width = (width - gutter) // 2
        caption_height = max(int(height * 0.15), min(int(height * 0.25), int(height * ratio)))
        image_height = height - caption_height
        primary = Rect(0, 0, card_width, image_height)
        secondary = Rect(card_width + gutter, 0, width - card_width - gutter, image_height)
        primary_caption = Rect(0, image_height, card_width, caption_height)
        secondary_caption = Rect(card_width + gutter, image_height, width - card_width - gutter, caption_height)
    else:
        card_height = (height - gutter) // 2
        caption_height = max(int(card_height * 0.15), min(int(card_height * 0.25), int(card_height * ratio)))
        image_height = card_height - caption_height
        primary = Rect(0, 0, width, image_height)
        secondary = Rect(0, card_height + gutter, width, image_height)
        primary_caption = Rect(0, image_height, width, caption_height)
        secondary_caption = Rect(0, card_height + gutter + image_height, width, caption_height)
    return LayoutGeometry(
        layout,
        effective_orientation,
        width,
        height,
        primary_photo=primary,
        secondary_photo=secondary,
        primary_caption=primary_caption,
        secondary_caption=secondary_caption,
        info=(primary_caption, secondary_caption),
        gutter=gutter,
    )
