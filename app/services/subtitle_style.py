from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.config import settings


def default_subtitle_style() -> dict[str, Any]:
    return {
        "chinese": {
            "font": settings.chinese_subtitle_font,
            "size": settings.chinese_subtitle_size,
            "color": settings.chinese_subtitle_color,
            "outline": settings.chinese_subtitle_outline,
        },
        "english": {
            "font": settings.english_subtitle_font,
            "size": settings.english_subtitle_size,
            "color": settings.english_subtitle_color,
            "outline": settings.english_subtitle_outline,
        },
        "layout": {
            "bottom_margin": settings.subtitle_bottom_margin,
            "line_gap": settings.subtitle_line_gap,
            "horizontal_margin": settings.subtitle_horizontal_margin,
            "play_res_x": 1920,
            "play_res_y": 1080,
        },
        "bottom_mask": {
            "height": settings.bottom_mask_height,
            "opacity": settings.bottom_mask_opacity,
        },
    }


def merge_subtitle_style(value: Any) -> dict[str, Any]:
    result = deepcopy(default_subtitle_style())
    if not isinstance(value, dict):
        return result
    for section in ("chinese", "english", "layout", "bottom_mask"):
        incoming = value.get(section)
        if isinstance(incoming, dict):
            result[section].update(incoming)
    # The ASS coordinate system is fixed, even if stale saved data says otherwise.
    result["layout"]["play_res_x"] = 1920
    result["layout"]["play_res_y"] = 1080
    return result
