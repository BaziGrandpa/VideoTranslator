from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.config import command_available, settings
from app.services.exact_preview import render_exact_preview
from app.services.renderer import _next_output_version, render_project

FFMPEG_AVAILABLE = command_available(settings.ffmpeg_bin)


def test_output_versions_do_not_overwrite_existing_video(tmp_path: Path) -> None:
    assert _next_output_version(tmp_path) == 1
    (tmp_path / "final-v001.mp4").write_bytes(b"existing")
    (tmp_path / "final-v004.mp4").write_bytes(b"existing")
    assert _next_output_version(tmp_path) == 5


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="FFmpeg is not installed")
def test_render_project_creates_playable_video(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    create = subprocess.run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert create.returncode == 0, create.stderr

    output = render_project(
        tmp_path,
        source,
        [
            {
                "start_ms": 250,
                "end_ms": 1400,
                "source_text": "Timing preview",
                "target_text": "Translated timing preview",
            },
            {
                "start_ms": 1500,
                "end_ms": 2750,
                "source_text": "Final render",
                "target_text": "Translated final render",
            },
        ],
        bilingual=True,
    )
    assert output.exists()
    assert output.stat().st_size > 10_000

    probe = subprocess.run(
        [
            settings.ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=avg_frame_rate,nb_frames",
            "-of",
            "json",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    details = json.loads(probe.stdout)
    assert 2.9 <= float(details["format"]["duration"]) <= 3.1
    assert details["streams"][0]["avg_frame_rate"] == "30/1"
    assert int(details["streams"][0]["nb_frames"]) == 90


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="FFmpeg is not installed")
def test_exact_preview_creates_png_with_shared_render_style(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    create = subprocess.run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:size=640x360:rate=30:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert create.returncode == 0, create.stderr

    preview = render_exact_preview(
        "preview-project",
        source,
        {
            "id": 1,
            "start_ms": 250,
            "end_ms": 1750,
            "source_text": "Exact FFmpeg preview",
            "target_text": "精确 FFmpeg 预览",
        },
        timestamp_ms=1000,
        bilingual=True,
        subtitle_style=None,
    )

    assert preview.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(preview) > 1000
