"""Single authoritative built-in rendering preset registry."""
from __future__ import annotations

DEFAULT_RENDER_PROFILE = "gdep073e01_6c"
DEFAULT_RENDER_DITHER = "gooddisplay"
DEFAULT_DEVICE_PANEL_PROFILE = "gdep073e01_6c"

SYSTEM_PRESETS = {
    "gooddisplay_spectra6": {
        "key": "gooddisplay_spectra6",
        "label_zh_tw": "微雪 7.3 吋 Spectra 6 原廠色彩與演算法",
        "description": "只影響系統預設與後續渲染；既有 Release 不變。",
        "settings": {"render.profile": DEFAULT_RENDER_PROFILE, "render.dither": DEFAULT_RENDER_DITHER, "render.dither_strength": 1.0, "render.color_distance": "rgb", "device.default_panel_profile": DEFAULT_DEVICE_PANEL_PROFILE},
        "compatible_panel_profiles": [DEFAULT_DEVICE_PANEL_PROFILE],
        "requires_device_confirmation": True,
        "renderer_version": "gooddisplay-spectra6-v1",
    }
}
