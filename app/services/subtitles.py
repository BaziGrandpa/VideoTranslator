from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.services.subtitle_text import display_target_text, normalize_source_text

TIMESTAMP_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\s+-->\s+"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})"
)
EXTRA_LINE_GAP = 30


def timestamp_to_ms(value: str) -> int:
    normalized = value.replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError(f"Invalid timestamp: {value}")
    return int((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)


def ms_to_srt(value: int) -> str:
    value = max(0, int(value))
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def ms_to_ass(value: int) -> str:
    value = max(0, int(value))
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    centiseconds = milliseconds // 10
    return f"{hours}:{minutes:02}:{seconds:02}.{centiseconds:02}"


def validate_segments(segments: list[dict[str, Any]]) -> list[str]:
    if not segments:
        return ["The project has no subtitles to render"]
    problems: list[str] = []
    previous: dict[str, Any] | None = None
    for index, segment in enumerate(segments, start=1):
        start_ms = int(segment["start_ms"])
        end_ms = int(segment["end_ms"])
        if start_ms < 0:
            problems.append(f"Subtitle {index} cannot have a negative start time")
        if end_ms <= start_ms:
            problems.append(f"Subtitle {index} must end after it starts")
        if not segment.get("chinese_only") and not str(segment.get("source_text", "")).strip():
            problems.append(f"Subtitle {index} has empty source text")
        if not display_target_text(str(segment.get("target_text", ""))):
            problems.append(f"Subtitle {index} has an empty translation")
        if previous and start_ms < int(previous["end_ms"]):
            problems.append(f"Subtitles {index - 1} and {index} overlap")
        previous = segment
    return problems


def write_srt(path: Path, segments: list[dict[str, Any]], field: str) -> None:
    blocks: list[str] = []
    for segment in segments:
        if field == "source_text" and segment.get("chinese_only"):
            continue
        raw_text = str(segment.get(field, ""))
        text = (
            display_target_text(raw_text)
            if field == "target_text"
            else normalize_source_text(raw_text)
        )
        blocks.append(
            f"{len(blocks) + 1}\n{ms_to_srt(segment['start_ms'])} --> {ms_to_srt(segment['end_ms'])}\n{text}"
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8-sig")


def ass_escape(value: str) -> str:
    return (
        value.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r\n", r"\N")
        .replace("\n", r"\N")
    )


def write_ass(
    path: Path,
    segments: list[dict[str, Any]],
    font_name: str | None = None,
    bilingual: bool = True,
    chinese_font: str = "Microsoft YaHei",
    chinese_size: int = 46,
    chinese_color: str = "#FFFFFF",
    chinese_outline: float = 3,
    chinese_margin: int = 112,
    english_font: str = "Arial",
    english_size: int = 28,
    english_color: str = "#D8D8D8",
    english_outline: float = 2,
    english_margin: int = 46,
    horizontal_margin: int = 90,
) -> None:
    if font_name:
        chinese_font = font_name

    def ass_color(value: str) -> str:
        match = re.fullmatch(r"#?([0-9a-fA-F]{6})", value.strip())
        if not match:
            raise ValueError(f"Invalid subtitle color: {value}")
        rgb = match.group(1)
        return f"&H00{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}".upper()

    header = f"""[Script Info]
Title: Video Translator bilingual subtitles
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: 1920
PlayResY: 1080
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Chinese,{chinese_font},{chinese_size},{ass_color(chinese_color)},&H000000FF,&H00101010,&H90000000,-1,0,0,0,100,100,1,0,1,{chinese_outline},0,2,{horizontal_margin},{horizontal_margin},{chinese_margin},1
Style: English,{english_font},{english_size},{ass_color(english_color)},&H000000FF,&H00101010,&H90000000,0,0,0,0,100,100,0,0,1,{english_outline},0,2,{horizontal_margin},{horizontal_margin},{english_margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    for segment in segments:
        start = ms_to_ass(segment["start_ms"])
        end = ms_to_ass(segment["end_ms"])
        translated = ass_escape(display_target_text(str(segment["target_text"])))
        chinese_only = bool(segment.get("chinese_only"))
        if chinese_only and bilingual:
            chinese_event_margin = english_margin
        elif segment.get("extra_line_gap") and bilingual:
            chinese_event_margin = chinese_margin + EXTRA_LINE_GAP
        else:
            chinese_event_margin = 0
        events.append(
            f"Dialogue: 1,{start},{end},Chinese,,0,0,{chinese_event_margin},,{translated}"
        )
        if bilingual and not chinese_only:
            source = ass_escape(normalize_source_text(str(segment["source_text"])))
            events.append(f"Dialogue: 1,{start},{end},English,,0,0,0,,{source}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")
