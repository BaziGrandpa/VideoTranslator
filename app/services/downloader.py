from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import settings

MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}


def _english_language_keys(info: dict[str, Any], kind: str) -> list[str]:
    values = info.get(kind) or {}
    return [key for key in values if key.lower() == "en" or key.lower().startswith("en-")]


def download_youtube(
    url: str,
    project_dir: Path,
    max_height: int,
    progress_callback: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("yt-dlp is missing; install the project dependencies first") from exc

    source_dir = project_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)

    def progress_hook(event: dict[str, Any]) -> None:
        if not progress_callback or event.get("status") != "downloading":
            return
        total = event.get("total_bytes") or event.get("total_bytes_estimate")
        downloaded = event.get("downloaded_bytes") or 0
        if total:
            progress_callback(min(48, 8 + int(downloaded / total * 40)))

    options: dict[str, Any] = {
        "noplaylist": True,
        "outtmpl": str(source_dir / "video.%(ext)s"),
        "format": f"bv*[height<=?{max_height}]+ba/b[height<=?{max_height}]",
        "merge_output_format": "mp4",
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "en.*"],
        "subtitlesformat": "vtt/best",
        "writeinfojson": False,
        "quiet": True,
        "no_warnings": False,
        "progress_hooks": [progress_hook],
        "js_runtimes": {"node": {}},
        "ffmpeg_location": str(Path(settings.ffmpeg_bin).parent),
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        clean_info = ydl.sanitize_info(info)

    (project_dir / "metadata.json").write_text(
        json.dumps(clean_info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    media_candidates = [
        path
        for path in source_dir.glob("video.*")
        if path.suffix.lower() in MEDIA_EXTENSIONS and not path.name.endswith(".part")
    ]
    if not media_candidates:
        raise RuntimeError("yt-dlp finished without producing a video file")
    video_path = max(media_candidates, key=lambda item: item.stat().st_size)

    subtitle_candidates = sorted(source_dir.glob("video*.vtt"))
    manual_keys = _english_language_keys(info, "subtitles")
    automatic_keys = _english_language_keys(info, "automatic_captions")
    subtitle_path = subtitle_candidates[0] if subtitle_candidates else None
    if subtitle_path and manual_keys:
        subtitle_source = "youtube_manual"
    elif subtitle_path and automatic_keys:
        subtitle_source = "youtube_auto"
    else:
        subtitle_source = None

    return {
        "youtube_id": info.get("id"),
        "title": info.get("title") or "Untitled video",
        "duration_ms": int(float(info.get("duration") or 0) * 1000),
        "video_path": video_path,
        "subtitle_path": subtitle_path,
        "subtitle_source": subtitle_source,
    }
