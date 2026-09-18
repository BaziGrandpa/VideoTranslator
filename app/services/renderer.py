from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import command_available, settings
from app.services.subtitle_style import merge_subtitle_style
from app.services.subtitles import validate_segments, write_ass, write_srt


def probe_video(video_path: Path) -> tuple[int, int]:
    command = [
        settings.ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(video_path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    streams = json.loads(result.stdout).get("streams") or []
    if not streams:
        raise RuntimeError("ffprobe could not read the video dimensions")
    return int(streams[0]["width"]), int(streams[0]["height"])


def write_styled_ass(
    path: Path,
    segments: list[dict[str, Any]],
    bilingual: bool,
    subtitle_style: dict[str, Any] | None,
) -> dict[str, Any]:
    style = merge_subtitle_style(subtitle_style)
    chinese = style["chinese"]
    english = style["english"]
    layout = style["layout"]
    write_ass(
        path,
        segments,
        bilingual=bilingual,
        chinese_font=chinese["font"],
        chinese_size=chinese["size"],
        chinese_color=chinese["color"],
        chinese_outline=chinese["outline"],
        chinese_margin=(
            layout["bottom_margin"] + layout["line_gap"] if bilingual else layout["bottom_margin"]
        ),
        english_font=english["font"],
        english_size=english["size"],
        english_color=english["color"],
        english_outline=english["outline"],
        english_margin=layout["bottom_margin"],
        horizontal_margin=layout["horizontal_margin"],
    )
    return style


def build_filter_graph(
    width: int,
    height: int,
    subtitle_style: dict[str, Any] | None,
    ass_filename: str = "bilingual.ass",
) -> str:
    style = merge_subtitle_style(subtitle_style)
    bottom_mask = style["bottom_mask"]
    mask_height = min(0.8, max(0.05, bottom_mask["height"]))
    mask_start = 1 - mask_height
    mask_alpha = round(255 * min(1, max(0, bottom_mask["opacity"])))
    return (
        f"color=c=black:s={width}x{height}:r=1:d=1,format=yuva420p,"
        "geq=lum='0':cb='128':cr='128':"
        f"a='if(gt(Y,H*{mask_start:.4f}),{mask_alpha}*"
        f"((Y-H*{mask_start:.4f})/(H*{mask_height:.4f})),0)'[shade];"
        "[0:v][shade]overlay=eof_action=repeat:shortest=0[masked];"
        f"[masked]ass=filename='{ass_filename}'[outv]"
    )


def render_project(
    project_dir: Path,
    video_path: Path,
    segments: list[dict[str, Any]],
    bilingual: bool,
    output_dir: Path | None = None,
    progress_callback: Callable[[int], None] | None = None,
    subtitle_style: dict[str, Any] | None = None,
) -> Path:
    if not command_available(settings.ffmpeg_bin):
        raise RuntimeError(
            "FFmpeg was not found; install a libass-enabled build and add it to PATH"
        )
    if not command_available(settings.ffprobe_bin):
        raise RuntimeError("ffprobe was not found; install the complete FFmpeg package")
    problems = validate_segments(segments)
    if problems:
        raise ValueError("；".join(problems[:10]))

    render_dir = project_dir / "render"
    output_dir = output_dir or project_dir / "output"
    render_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    ass_path = render_dir / "bilingual.ass"
    style = write_styled_ass(ass_path, segments, bilingual, subtitle_style)
    version = _next_output_version(output_dir)
    version_suffix = f"v{version:03d}"
    write_srt(output_dir / f"english-{version_suffix}.srt", segments, "source_text")
    write_srt(output_dir / f"chinese-{version_suffix}.srt", segments, "translated_text")

    width, height = probe_video(video_path)
    # Generate the gradient once and repeat that frame. Applying geq to the source
    # video would recalculate every pixel of every frame and make 1080p renders slow.
    filter_graph = build_filter_graph(width, height, style)
    filter_script = render_dir / "filter.ffscript"
    filter_script.write_text(filter_graph, encoding="utf-8")
    output_path = output_dir / f"final-{version_suffix}.mp4"
    command = [
        settings.ffmpeg_bin,
        "-y",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-i",
        str(video_path),
        "-/filter_complex",
        str(filter_script),
        "-map",
        "[outv]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-crf",
        "20",
        "-preset",
        "medium",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    process = subprocess.Popen(
        command,
        cwd=render_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    duration_ms = max(int(segment["end_ms"]) for segment in segments)
    assert process.stdout is not None
    for raw_line in process.stdout:
        key, separator, value = raw_line.strip().partition("=")
        if separator and key in {"out_time_us", "out_time_ms"} and value.isdigit():
            encoded_ms = int(value) // 1000
            percent = min(99, round(encoded_ms * 100 / max(1, duration_ms)))
            if progress_callback:
                progress_callback(percent)
    stderr = process.stderr.read() if process.stderr is not None else ""
    return_code = process.wait()
    if return_code != 0:
        tail = "\n".join(stderr.splitlines()[-20:])
        raise RuntimeError(f"FFmpeg rendering failed:\n{tail}")
    if progress_callback:
        progress_callback(100)
    return output_path


def _next_output_version(output_dir: Path) -> int:
    versions = []
    for path in output_dir.glob("final-v*.mp4"):
        match = re.fullmatch(r"final-v(\d+)\.mp4", path.name, re.IGNORECASE)
        if match and path.stat().st_size > 0:
            versions.append(int(match.group(1)))
    return max(versions, default=0) + 1
