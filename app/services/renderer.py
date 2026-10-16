from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.cards.render import append_card_filters, prepare_card_images
from app.config import command_available, settings
from app.services.subtitle_style import merge_subtitle_style
from app.services.subtitles import validate_segments, write_ass, write_srt


class RenderCancelled(Exception):
    """The user stopped an active render."""


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


def video_encoding_args() -> list[str]:
    encoder = settings.render_video_encoder
    if encoder == "h264_nvenc":
        return [
            "-c:v", encoder, "-pix_fmt", "yuv420p", "-profile:v", "high",
            "-preset", "p4", "-rc", "vbr", "-cq", "20", "-b:v", "0",
        ]
    if encoder == "h264_qsv":
        return [
            "-c:v", encoder, "-pix_fmt", "nv12", "-profile:v", "high",
            "-preset", "medium", "-global_quality", "20",
        ]
    if encoder == "libx264":
        return [
            "-c:v", encoder, "-pix_fmt", "yuv420p", "-profile:v", "high",
            "-crf", "20", "-preset", "medium",
        ]
    raise ValueError(f"Unsupported render video encoder: {encoder}")


def render_project(
    project_dir: Path,
    video_path: Path,
    segments: list[dict[str, Any]],
    bilingual: bool,
    output_dir: Path | None = None,
    progress_callback: Callable[[int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    subtitle_style: dict[str, Any] | None = None,
    cards: list[dict[str, Any]] | None = None,
) -> Path:
    problems = validate_segments(segments)
    if problems:
        raise ValueError("；".join(problems[:10]))
    duration_ms = max(int(segment["end_ms"]) for segment in segments)
    if not command_available(settings.ffmpeg_bin):
        raise RuntimeError(
            "FFmpeg was not found; install a libass-enabled build and add it to PATH"
        )
    if not command_available(settings.ffprobe_bin):
        raise RuntimeError("ffprobe was not found; install the complete FFmpeg package")
    render_dir = project_dir / "render"
    output_dir = output_dir or project_dir / "output"
    render_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    ass_path = render_dir / "bilingual.ass"
    style = write_styled_ass(ass_path, segments, bilingual, subtitle_style)
    version = _next_output_version(output_dir)
    version_suffix = f"v{version:03d}"

    width, height = probe_video(video_path)
    # Generate the gradient once and repeat that frame. Applying geq to the source
    # video would recalculate every pixel of every frame and make 1080p renders slow.
    prepared_cards = prepare_card_images(render_dir, cards or [], width, height)
    filter_graph = append_card_filters(
        build_filter_graph(width, height, style), prepared_cards
    )
    filter_script = render_dir / "filter.ffscript"
    filter_script.write_text(filter_graph, encoding="utf-8")
    output_path = output_dir / f"final-{version_suffix}.mp4"
    temporary_output_path = output_dir / f".rendering-{version_suffix}.mp4"
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
        *[
            argument
            for item in prepared_cards
            for argument in ("-loop", "1", "-framerate", "1", "-i", str(item["path"]))
        ],
        "-/filter_complex",
        str(filter_script),
        "-map",
        "[outv]",
        "-map",
        "0:a?",
        *video_encoding_args(),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(temporary_output_path),
    ]
    if should_cancel and should_cancel():
        raise RenderCancelled("Render cancelled")
    process = None
    completed = False
    published_video = False
    attempted_srt_paths: list[Path] = []
    try:
        process = subprocess.Popen(
            command,
            cwd=render_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for raw_line in process.stdout:
            if should_cancel and should_cancel():
                raise RenderCancelled("Render cancelled")
            key, separator, value = raw_line.strip().partition("=")
            if separator and key in {"out_time_us", "out_time_ms"} and value.isdigit():
                encoded_ms = int(value) // 1000
                percent = min(99, round(encoded_ms * 100 / max(1, duration_ms)))
                if progress_callback:
                    progress_callback(percent)
        stderr = process.stderr.read() if process.stderr is not None else ""
        return_code = process.wait()
        if should_cancel and should_cancel():
            raise RenderCancelled("Render cancelled")
        if return_code != 0:
            tail = "\n".join(stderr.splitlines()[-20:])
            raise RuntimeError(f"FFmpeg rendering failed:\n{tail}")
        for language, field in (("english", "source_text"), ("chinese", "target_text")):
            srt_path = output_dir / f"{language}-{version_suffix}.srt"
            attempted_srt_paths.append(srt_path)
            write_srt(srt_path, segments, field)
        temporary_output_path.replace(output_path)
        published_video = True
        if should_cancel and should_cancel():
            raise RenderCancelled("Render cancelled")
        if progress_callback:
            progress_callback(100)
        completed = True
        return output_path
    finally:
        # Any exception after spawning must reap FFmpeg, including callback and I/O errors.
        # Close its pipes before removing this attempt's files on Windows.
        try:
            if process is not None:
                try:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                finally:
                    if process.stdout is not None:
                        process.stdout.close()
                    if process.stderr is not None:
                        process.stderr.close()
        finally:
            if not completed:
                temporary_output_path.unlink(missing_ok=True)
                if published_video:
                    output_path.unlink(missing_ok=True)
                for srt_path in attempted_srt_paths:
                    srt_path.unlink(missing_ok=True)


def _next_output_version(output_dir: Path) -> int:
    versions = []
    for path in output_dir.glob("final-v*.mp4"):
        match = re.fullmatch(r"final-v(\d+)\.mp4", path.name, re.IGNORECASE)
        if match and path.stat().st_size > 0:
            versions.append(int(match.group(1)))
    return max(versions, default=0) + 1
