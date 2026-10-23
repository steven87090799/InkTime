"""Single authoritative built-in rendering preset registry."""

from __future__ import annotations

from .natural_d import VERSION as NATURAL_D_VERSION

DEFAULT_RENDER_PROFILE = "gdep073e01_6c"
DEFAULT_RENDER_DITHER = "natural_d"
DEFAULT_DEVICE_PANEL_PROFILE = "gdep073e01_6c"

SYSTEM_PRESETS = {
    "gooddisplay_spectra6": {
        "key": "gooddisplay_spectra6",
        "label_zh_tw": "微雪 7.3 吋 Spectra 6 原廠色彩與演算法",
        "description": "只影響系統預設與後續渲染；既有 Release 不變。",
        "settings": {
            "render.profile": DEFAULT_RENDER_PROFILE,
            "render.dither": "gooddisplay",
            "render.dither_strength": 1.0,
            "render.color_distance": "rgb",
            "device.default_panel_profile": DEFAULT_DEVICE_PANEL_PROFILE,
        },
        "compatible_panel_profiles": [DEFAULT_DEVICE_PANEL_PROFILE],
        "requires_device_confirmation": True,
        "renderer_version": "gooddisplay-spectra6-v1",
    },
    "warm_vivid_c": {
        "key": "warm_vivid_c",
        "label_zh_tw": "C 微暖增豔",
        "description": "保留中性高光，輕度增豔與微暖；原版仍可切換，既有照片與 Release 不變。",
        "settings": {
            "render.profile": DEFAULT_RENDER_PROFILE,
            "render.dither": "warm_vivid_c",
            "render.dither_strength": 1.0,
            "render.color_distance": "rgb",
            "device.default_panel_profile": DEFAULT_DEVICE_PANEL_PROFILE,
        },
        "compatible_panel_profiles": [DEFAULT_DEVICE_PANEL_PROFILE],
        "requires_device_confirmation": True,
        "renderer_version": "warm-vivid-c-v1",
    },
    "natural_d": {
        "key": "natural_d",
        "label_zh_tw": "D 自然膚色（預設）",
        "description": "飽和度微降 5%，輕微白平衡修正與亮部保白；適用後續預覽與渲染。",
        "settings": {
            "render.profile": DEFAULT_RENDER_PROFILE,
            "render.dither": DEFAULT_RENDER_DITHER,
            "render.dither_strength": 1.0,
            "render.color_distance": "rgb",
            "device.default_panel_profile": DEFAULT_DEVICE_PANEL_PROFILE,
        },
        "compatible_panel_profiles": [DEFAULT_DEVICE_PANEL_PROFILE],
        "requires_device_confirmation": True,
        "renderer_version": NATURAL_D_VERSION,
    },
}
