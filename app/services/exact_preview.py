from __future__ import annotations

import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from app.config import command_available, settings
from app.services.renderer import build_filter_graph, probe_video, write_styled_ass


class PreviewCancelled(RuntimeError):
    pass


class PreviewProcessRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, tuple[str, subprocess.Popen[bytes] | None]] = {}

    def reserve(self, project_id: str) -> str:
        request_id = str(uuid.uuid4())
        with self._lock:
            previous = self._active.get(project_id)
            self._active[project_id] = (request_id, None)
        if previous and previous[1] is not None:
            _terminate(previous[1])
        return request_id

    def attach(self, project_id: str, request_id: str, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            current = self._active.get(project_id)
            if current is None or current[0] != request_id:
                _terminate(process)
                raise PreviewCancelled("Exact preview was replaced by a newer request")
            self._active[project_id] = (request_id, process)

    def finish(self, project_id: str, request_id: str) -> None:
        with self._lock:
            current = self._active.get(project_id)
            if current and current[0] == request_id:
                self._active.pop(project_id, None)

    def is_current(self, project_id: str, request_id: str) -> bool:
        with self._lock:
            current = self._active.get(project_id)
            return bool(current and current[0] == request_id)

    def cancel(self, project_id: str) -> bool:
        with self._lock:
            current = self._active.pop(project_id, None)
        if current and current[1] is not None:
            _terminate(current[1])
        return current is not None


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        except OSError:
            pass


preview_processes = PreviewProcessRegistry()


def render_exact_preview(
    project_id: str,
    video_path: Path,
    segment: dict[str, Any],
    timestamp_ms: int,
    bilingual: bool,
    subtitle_style: dict[str, Any] | None,
) -> bytes:
    if not command_available(settings.ffmpeg_bin):
        raise RuntimeError("FFmpeg was not found")
    if not command_available(settings.ffprobe_bin):
        raise RuntimeError("ffprobe was not found")

    request_id = preview_processes.reserve(project_id)
    try:
        with tempfile.TemporaryDirectory(prefix="video-translator-preview-") as temp_name:
            temp_dir = Path(temp_name)
            ass_path = temp_dir / "bilingual.ass"
            preview_segment = {
                **segment,
                "start_ms": 0,
                "end_ms": 86_400_000,
            }
            style = write_styled_ass(ass_path, [preview_segment], bilingual, subtitle_style)
            width, height = probe_video(video_path)
            filter_script = temp_dir / "filter.ffscript"
            filter_script.write_text(build_filter_graph(width, height, style), encoding="utf-8")
            output_path = temp_dir / "exact-preview.png"
            command = [
                settings.ffmpeg_bin,
                "-y",
                "-loglevel",
                "error",
                "-ss",
                f"{max(0, timestamp_ms) / 1000:.3f}",
                "-i",
                str(video_path),
                "-/filter_complex",
                str(filter_script),
                "-map",
                "[outv]",
                "-frames:v",
                "1",
                str(output_path),
            ]
            process = subprocess.Popen(
                command,
                cwd=temp_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            preview_processes.attach(project_id, request_id, process)
            _, stderr = process.communicate(timeout=60)
            if not preview_processes.is_current(project_id, request_id):
                raise PreviewCancelled("Exact preview was replaced by a newer request")
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace").strip()
                if process.returncode < 0:
                    raise PreviewCancelled("Exact preview was cancelled")
                raise RuntimeError(f"FFmpeg exact preview failed: {detail[-2000:]}")
            if not output_path.exists():
                raise RuntimeError("FFmpeg did not produce an exact preview image")
            return output_path.read_bytes()
    except subprocess.TimeoutExpired as exc:
        preview_processes.cancel(project_id)
        raise RuntimeError("FFmpeg exact preview timed out after 60 seconds") from exc
    finally:
        preview_processes.finish(project_id, request_id)
