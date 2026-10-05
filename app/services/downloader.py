from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import settings

MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}


def _english_caption(info: dict[str, Any]) -> tuple[str | None, str | None]:
    """Choose one original English track, never an auto-translated track."""
    manual = info.get("subtitles") or {}
    for key in ("en", "en-US", "en-GB"):
        if key in manual:
            return key, "youtube_manual"
    for key in manual:
        if key.lower().startswith("en-"):
            return key, "youtube_manual"

    automatic = info.get("automatic_captions") or {}
    if "en-orig" in automatic:
        return "en-orig", "youtube_auto"
    language = str(info.get("language") or "").lower()
    if language == "en" or language.startswith("en-"):
        for key in (info.get("language"), "en"):
            if key in automatic:
                return key, "youtube_auto"
    return None, None


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
        "writesubtitles": False,
        "writeautomaticsub": False,
        "subtitlesformat": "vtt/best",
        "writeinfojson": False,
        "quiet": True,
        "no_warnings": False,
        "progress_hooks": [progress_hook],
        "js_runtimes": {"node": {}},
        "ffmpeg_location": str(Path(settings.ffmpeg_bin).parent),
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        metadata = ydl.extract_info(url, download=False)
        caption_key, subtitle_source = _english_caption(metadata)
        if caption_key:
            ydl.params.update({
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": [caption_key],
            })
        try:
            info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as exc:
            if not caption_key or "Unable to download video subtitles" not in str(exc):
                raise
            # The video may still be available when the caption endpoint is blocked.
            # Download it without captions and let the pipeline use WhisperX.
            ydl.params.update({"writesubtitles": False, "writeautomaticsub": False})
            info = ydl.extract_info(url, download=True)
            caption_key, subtitle_source = None, None
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

    subtitle_path = source_dir / f"video.{caption_key}.vtt" if caption_key else None
    if subtitle_path is None or not subtitle_path.is_file():
        subtitle_path = None
        subtitle_source = None

    return {
        "youtube_id": info.get("id"),
        "title": info.get("title") or "Untitled video",
        "duration_ms": int(float(info.get("duration") or 0) * 1000),
        "video_path": video_path,
        "subtitle_path": subtitle_path,
        "subtitle_source": subtitle_source,
    }
