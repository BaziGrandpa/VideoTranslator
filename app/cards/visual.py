from __future__ import annotations

import io
import math
import os
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from app.cards.model import CardPayload, CardStyle

SIDE_MARGIN_RATIO = 0.04
TOP_MARGIN_RATIO = 0.05
MAX_HEIGHT_RATIO = 0.75

WINDOWS_FONTS = {
    "Microsoft YaHei": ("msyh.ttc", "msyhbd.ttc"),
    "Arial": ("arial.ttf", "arialbd.ttf"),
    "SimHei": ("simhei.ttf", "simhei.ttf"),
}


def _font(family: str, size: int, bold: bool) -> ImageFont.FreeTypeFont:
    normal, heavy = WINDOWS_FONTS[family]
    filename = heavy if bold else normal
    candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / filename,
        Path("/usr/share/fonts/truetype/dejavu")
        / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.truetype("DejaVuSans.ttf", size)


def _tokens(value: str) -> list[str]:
    return re.findall(r"[^\x00-\x7f]|[\x21-\x7e]+\s*|\s+", value)


def _wrap(draw: ImageDraw.ImageDraw, value: str, font: ImageFont.FreeTypeFont,
          max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in value.replace("\r\n", "\n").split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for token in _tokens(paragraph):
            candidate = (current + token).lstrip()
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current.rstrip())
                current = ""
            if draw.textlength(token, font=font) <= max_width:
                current = token.lstrip()
            else:
                for char in token:
                    candidate = current + char
                    if current and draw.textlength(candidate, font=font) > max_width:
                        lines.append(current.rstrip())
                        current = ""
                    current += char
        lines.append(current.rstrip())
    return lines


def fade_factor(card: dict[str, Any] | CardPayload, timestamp_ms: int) -> float:
    data = card.model_dump() if isinstance(card, CardPayload) else card
    start, end = int(data["start_ms"]), int(data["end_ms"])
    if timestamp_ms < start or timestamp_ms >= end:
        return 0.0
    style = CardStyle.model_validate(data["style"])
    factor = 1.0
    if style.fade_in_ms:
        factor = min(factor, (timestamp_ms - start) / style.fade_in_ms)
    if style.fade_out_ms:
        factor = min(factor, (end - timestamp_ms) / style.fade_out_ms)
    return max(0.0, min(1.0, factor))


def render_card_image(
    card: dict[str, Any] | CardPayload, video_width: int, video_height: int,
    opacity: float = 1.0,
) -> tuple[Image.Image, dict[str, int]]:
    data = card.model_dump() if isinstance(card, CardPayload) else card
    style = CardStyle.model_validate(data["style"])
    unit = video_height / 1080
    width = round(video_width * style.width_pct / 100)
    padding = max(1, round(style.padding * unit))
    inside = width - 2 * padding
    if inside <= 0:
        raise ValueError("Card width is too small for its padding")
    scratch = Image.new("RGBA", (1, 1))
    measure = ImageDraw.Draw(scratch)
    blocks = []
    for key, size, color, bold in (
        ("chinese_term", style.title_size, style.title_color, style.title_bold),
        ("english_term", style.english_size, style.english_color, style.english_bold),
        ("explanation", style.body_size, style.body_color, style.body_bold),
    ):
        value = str(data[key]).strip()
        if not value:
            continue
        font = _font(style.font_family, max(1, round(size * unit)), bold)
        lines = _wrap(measure, value, font, inside)
        line_height = max(1, math.ceil(font.size * style.line_spacing * 1.12))
        blocks.append((lines, font, color, line_height))
    gap = round(12 * unit)
    height = 2 * padding + sum(len(lines) * line_height for lines, _, _, line_height in blocks)
    height += max(0, len(blocks) - 1) * gap
    allowed = round(video_height * MAX_HEIGHT_RATIO)
    if height > allowed:
        raise ValueError(
            f"Card needs {height}px of height, but the safe area allows {allowed}px; "
            "increase its width, shorten the text, or reduce font size"
        )
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=max(1, round(18 * unit)),
        fill=(0, 0, 0, round(255 * style.background_opacity)),
    )
    y = padding
    for index, (lines, font, color, line_height) in enumerate(blocks):
        if index:
            y += gap
        for line in lines:
            draw.text((padding, y), line, font=font, fill=color, anchor="lt")
            y += line_height
    if opacity < 1:
        alpha = image.getchannel("A").point(lambda value: round(value * opacity))
        image.putalpha(alpha)
    side_margin = round(video_width * SIDE_MARGIN_RATIO)
    if style.side == "left":
        x = side_margin
    elif style.side == "center":
        x = (video_width - width) // 2
    else:
        x = video_width - side_margin - width
    return image, {
        "x": x,
        "y": round(video_height * TOP_MARGIN_RATIO),
        "width": width,
        "height": height,
    }


def render_card_png(
    card: dict[str, Any] | CardPayload, video_width: int, video_height: int,
    opacity: float = 1.0,
) -> tuple[bytes, dict[str, int]]:
    image, geometry = render_card_image(card, video_width, video_height, opacity)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue(), geometry
