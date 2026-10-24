from __future__ import annotations

import io
import math
import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pydantic import BaseModel, Field, model_validator

SIZES = {"16x9": (1536, 864), "4x3": (1536, 1152)}
WINDOWS_FONTS = {
    "Microsoft YaHei": ("msyh.ttc", "msyhbd.ttc"),
    "SimHei": ("simhei.ttf", "simhei.ttf"),
    "SimSun": ("simsun.ttc", "simsun.ttc"),
}


class CoverDesign(BaseModel):
    title: str = Field(default="", max_length=120)
    highlight_terms: list[str] = Field(default_factory=list, max_length=12)
    font_family: str = "Microsoft YaHei"
    font_size: int = Field(default=104, ge=24, le=240)
    font_weight: str = "bold"
    line_spacing: float = Field(default=1.04, ge=0.75, le=1.8)
    x_pct: float = Field(default=5.5, ge=0, le=90)
    y_pct: float = Field(default=28, ge=0, le=90)
    text_color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    highlight_color: str = Field(default="#E0C077", pattern=r"^#[0-9A-Fa-f]{6}$")
    mask_solid_pct: float = Field(default=18, ge=0, le=90)
    mask_width_pct: float = Field(default=67, ge=10, le=100)
    mask_opacity: float = Field(default=0.86, ge=0, le=1)
    mask_gamma: float = Field(default=1.25, ge=0.2, le=4)
    shadow_opacity: float = Field(default=0.62, ge=0, le=3)
    shadow_blur: int = Field(default=9, ge=0, le=45)
    shadow_dx: int = Field(default=3, ge=-30, le=30)
    shadow_dy: int = Field(default=5, ge=-30, le=30)

    @model_validator(mode="after")
    def validate_options(self) -> CoverDesign:
        if self.font_family not in WINDOWS_FONTS:
            raise ValueError("Unsupported font family")
        if self.font_weight not in {"regular", "bold"}:
            raise ValueError("Unsupported font weight")
        if self.mask_solid_pct >= self.mask_width_pct:
            raise ValueError("The mask width must exceed its solid region")
        if any(not term.strip() or len(term) > 50 for term in self.highlight_terms):
            raise ValueError("Highlight terms must contain 1–50 characters")
        return self


def default_design(ratio: str) -> dict[str, Any]:
    if ratio == "4x3":
        return CoverDesign(font_size=114, y_pct=30, mask_width_pct=72).model_dump()
    return CoverDesign().model_dump()


def title_line_count(title: str) -> int:
    """Count explicit title lines; browser wrapping does not create a preset."""
    return len(title.replace("\r\n", "\n").replace("\r", "\n").split("\n"))


def style_line_count(title: str) -> int:
    """Titles above four lines reuse the four-line style slot."""
    return min(title_line_count(title), 4)


def design_style(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key != "title"}


def _font(family: str, size: int, weight: str) -> ImageFont.FreeTypeFont:
    regular, bold = WINDOWS_FONTS[family]
    filename = bold if weight == "bold" else regular
    candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / filename,
        Path("/usr/share/fonts/truetype/dejavu")
        / ("DejaVuSans-Bold.ttf" if weight == "bold" else "DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.truetype("DejaVuSans.ttf", size)


def _highlight_flags(line: str, terms: list[str]) -> list[bool]:
    flags = [False] * len(line)
    for term in terms:
        start = 0
        while (index := line.find(term, start)) >= 0:
            flags[index:index + len(term)] = [True] * len(term)
            start = index + len(term)
    return flags


def _mask(image: Image.Image, design: CoverDesign) -> None:
    width, height = image.size
    solid = round(width * design.mask_solid_pct / 100)
    end = max(solid + 1, round(width * design.mask_width_pct / 100))
    strip = Image.new("RGBA", (width, 1), (0, 0, 0, 0))
    pixels = strip.load()
    for x in range(min(width, end)):
        fade = 1.0 if x <= solid else (1 - (x - solid) / (end - solid)) ** design.mask_gamma
        pixels[x, 0] = (0, 0, 0, round(255 * design.mask_opacity * fade))
    image.alpha_composite(strip.resize((width, height)))


def _composite_tinted_mask(
    image: Image.Image, mask: Image.Image, color: str, opacity: float
) -> None:
    if opacity <= 0:
        return
    layer = Image.new("RGBA", image.size, color)
    layer.putalpha(mask.point(lambda value: round(value * opacity)))
    image.alpha_composite(layer)


def render_cover(image_path: Path, ratio: str, values: dict[str, Any]) -> bytes:
    design = CoverDesign.model_validate(values)
    if not design.title.strip():
        raise ValueError("Enter a cover title")
    with Image.open(image_path) as original:
        image = original.convert("RGBA")
    expected = SIZES[ratio]
    if image.size != expected:
        image = image.resize(expected, Image.Resampling.LANCZOS)
    _mask(image, design)
    font = _font(design.font_family, design.font_size, design.font_weight)
    synthetic_bold = 1 if (design.font_weight == "bold" and
                           WINDOWS_FONTS[design.font_family][0] ==
                           WINDOWS_FONTS[design.font_family][1]) else 0
    scratch = ImageDraw.Draw(image)
    title_line_count(design.title)
    lines = design.title.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    line_step = math.ceil(design.font_size * design.line_spacing)
    x = round(image.width * design.x_pct / 100)
    y = round(image.height * design.y_pct / 100)
    text_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    text_draw = ImageDraw.Draw(text_layer)
    soft_shadow = Image.new("L", image.size)
    soft_draw = ImageDraw.Draw(soft_shadow)
    firm_shadow = Image.new("L", image.size)
    firm_draw = ImageDraw.Draw(firm_shadow)
    glyphs: list[tuple[tuple[int, int], str]] = []
    # Large type needs a visibly separated cast shadow, especially over the
    # dark left-side mask. These are shifted glyph silhouettes, not a stroke.
    offset_scale = max(1.0, design.font_size / 32)
    dx = round(design.shadow_dx * offset_scale)
    dy = round(design.shadow_dy * offset_scale)
    for line_number, line in enumerate(lines):
        line_y = y + line_number * line_step
        if line_y + design.font_size > image.height - 12:
            raise ValueError("The cover title extends below the image")
        if x + scratch.textlength(line, font=font) > image.width - 12:
            raise ValueError("A cover title line extends past the right edge")
        flags = _highlight_flags(line, design.highlight_terms)
        cursor = float(x)
        for char, highlighted in zip(line, flags, strict=True):
            color = design.highlight_color if highlighted else design.text_color
            text_draw.text(
                (round(cursor), line_y), char, font=font, fill=color,
                stroke_width=synthetic_bold, stroke_fill=color,
            )
            position = (round(cursor), line_y)
            glyphs.append((position, char))
            firm_draw.text(
                (position[0] + dx, position[1] + dy), char, font=font, fill=255,
                stroke_width=synthetic_bold, stroke_fill=255,
            )
            soft_draw.text(
                (position[0] + round(dx * 1.5), position[1] + round(dy * 1.5)),
                char, font=font, fill=255,
                stroke_width=synthetic_bold, stroke_fill=255,
            )
            cursor += scratch.textlength(char, font=font)
    if design.shadow_opacity:
        opacity = min(1.0, design.shadow_opacity)
        soft_shadow = soft_shadow.filter(ImageFilter.GaussianBlur(design.shadow_blur))
        _composite_tinted_mask(image, soft_shadow, "#000000", opacity)
        if design.shadow_blur:
            firm_shadow = firm_shadow.filter(ImageFilter.GaussianBlur(
                max(1, design.shadow_blur // 8)
            ))
        _composite_tinted_mask(image, firm_shadow, "#000000", opacity)
        # Black is already opaque at 100%. Boost the soft falloff nearby;
        # distant, sharp glyph copies look like stale text after dragging.
        for index, distance in enumerate((1.25, 1.5)):
            extra_opacity = min(1.0, max(0.0, design.shadow_opacity - 1 - index))
            if extra_opacity <= 0:
                break
            extra_shadow = Image.new("L", image.size)
            extra_draw = ImageDraw.Draw(extra_shadow)
            for position, char in glyphs:
                extra_draw.text(
                    (position[0] + round(dx * distance),
                     position[1] + round(dy * distance)),
                    char, font=font, fill=255,
                    stroke_width=synthetic_bold, stroke_fill=255,
                )
            extra_shadow = extra_shadow.filter(ImageFilter.GaussianBlur(
                max(8, design.shadow_blur // 2)
            ))
            _composite_tinted_mask(image, extra_shadow, "#000000", extra_opacity)
    image.alpha_composite(text_layer)
    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG")
    return output.getvalue()
