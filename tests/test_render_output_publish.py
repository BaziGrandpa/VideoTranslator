from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest

from app.services import renderer
from app.services.subtitle_style import merge_subtitle_style


@pytest.mark.parametrize("return_code", [0, 1])
def test_render_publishes_only_completed_video(
    tmp_path: Path, monkeypatch, return_code: int
) -> None:
    monkeypatch.setattr(renderer, "command_available", lambda _command: True)
    monkeypatch.setattr(renderer, "validate_segments", lambda _segments: [])
    monkeypatch.setattr(renderer, "probe_video", lambda _path: (1280, 720))
    monkeypatch.setattr(
        renderer,
        "write_styled_ass",
        lambda *_args, **_kwargs: merge_subtitle_style(None),
    )
    monkeypatch.setattr(
        renderer,
        "write_srt",
        lambda path, *_args: path.write_text("subtitle", encoding="utf-8"),
    )

    class FakeProcess:
        def __init__(self, command, **_kwargs):
            Path(command[-1]).write_bytes(b"video bytes")
            self.stdout = StringIO("")
            self.stderr = StringIO("encoding failed")

        def wait(self):
            return return_code

        def poll(self):
            return return_code

    monkeypatch.setattr(renderer.subprocess, "Popen", FakeProcess)
    output_dir = tmp_path / "output"
    render_args = (
        tmp_path / "project",
        tmp_path / "source.mp4",
        [{"start_ms": 0, "end_ms": 1000, "source_text": "Hello", "target_text": "你好"}],
        True,
    )

    if return_code:
        with pytest.raises(RuntimeError, match="FFmpeg rendering failed"):
            renderer.render_project(*render_args, output_dir=output_dir)
        assert list(output_dir.iterdir()) == []
    else:
        result = renderer.render_project(*render_args, output_dir=output_dir)
        assert result == output_dir / "final-v001.mp4"
        assert result.read_bytes() == b"video bytes"
        assert (output_dir / "english-v001.srt").exists()
        assert (output_dir / "chinese-v001.srt").exists()
        assert not (output_dir / ".rendering-v001.mp4").exists()


def test_cancel_render_stops_ffmpeg_and_removes_partial_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(renderer, "command_available", lambda _command: True)
    monkeypatch.setattr(renderer, "validate_segments", lambda _segments: [])
    monkeypatch.setattr(renderer, "probe_video", lambda _path: (1280, 720))
    monkeypatch.setattr(
        renderer, "write_styled_ass", lambda *_args, **_kwargs: merge_subtitle_style(None)
    )
    processes = []

    class FakeProcess:
        def __init__(self, command, **_kwargs):
            Path(command[-1]).write_bytes(b"incomplete video")
            self.stdout = StringIO("out_time_us=500000\n")
            self.stderr = StringIO("")
            self.terminated = False
            processes.append(self)

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(renderer.subprocess, "Popen", FakeProcess)
    checks = 0

    def should_cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    output_dir = tmp_path / "output"
    with pytest.raises(renderer.RenderCancelled):
        renderer.render_project(
            tmp_path / "project",
            tmp_path / "source.mp4",
            [{"start_ms": 0, "end_ms": 1000, "source_text": "Hello", "target_text": "你好"}],
            True,
            output_dir=output_dir,
            should_cancel=should_cancel,
        )
    assert processes[0].terminated
    assert list(output_dir.iterdir()) == []


def test_nvidia_render_uses_nvenc_quality_parameters(monkeypatch) -> None:
    monkeypatch.setattr(
        renderer, "settings", replace(renderer.settings, render_video_encoder="h264_nvenc")
    )
    assert renderer.video_encoding_args() == [
        "-c:v", "h264_nvenc", "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-preset", "p4", "-rc", "vbr", "-cq", "20", "-b:v", "0",
    ]


def test_intel_render_uses_qsv_quality_parameters(monkeypatch) -> None:
    monkeypatch.setattr(
        renderer, "settings", replace(renderer.settings, render_video_encoder="h264_qsv")
    )
    assert renderer.video_encoding_args() == [
        "-c:v", "h264_qsv", "-pix_fmt", "nv12", "-profile:v", "high",
        "-preset", "medium", "-global_quality", "20",
    ]


def test_empty_render_is_rejected_before_starting_any_process(tmp_path: Path, monkeypatch) -> None:
    def unexpected_process(*_args, **_kwargs):
        pytest.fail("An empty render must not start FFmpeg or ffprobe")

    monkeypatch.setattr(renderer.subprocess, "Popen", unexpected_process)
    with pytest.raises(ValueError, match="no subtitles to render"):
        renderer.render_project(tmp_path, tmp_path / "source.mp4", [], True)
    assert not (tmp_path / "render").exists()
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("failure,slow_exit", [
    ("progress", False),
    ("progress", True),
    ("cancel_check", False),
    ("srt", False),
    ("final_progress", False),
])
def test_render_exception_cleans_process_and_only_current_outputs(
    tmp_path: Path, monkeypatch, failure: str, slow_exit: bool,
) -> None:
    monkeypatch.setattr(renderer, "command_available", lambda _command: True)
    monkeypatch.setattr(renderer, "probe_video", lambda _path: (640, 360))
    processes = []

    class FakeProcess:
        def __init__(self, command, **_kwargs):
            Path(command[-1]).write_bytes(b"new video")
            self.stdout = StringIO("out_time_us=500000\n")
            self.stderr = StringIO("")
            self.returncode = None
            self.terminated = False
            self.killed = False
            self.waited = False
            processes.append(self)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            if timeout is not None and slow_exit:
                raise renderer.subprocess.TimeoutExpired("ffmpeg", timeout)
            self.waited = True
            self.returncode = 0
            return 0

    monkeypatch.setattr(renderer.subprocess, "Popen", FakeProcess)

    def progress(value):
        if failure == "progress" or (failure == "final_progress" and value == 100):
            raise RuntimeError("Injected progress failure")

    def cancel_check():
        if failure == "cancel_check" and processes:
            raise RuntimeError("Injected cancellation check failure")
        return False

    def write_srt(path, _segments, field):
        path.write_text("subtitle", encoding="utf-8")
        if failure == "srt" and field == "target_text":
            raise RuntimeError("Injected subtitle write failure")

    monkeypatch.setattr(renderer, "write_srt", write_srt)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    previous_files = {name: b"previous output" for name in (
        "final-v001.mp4", "english-v001.srt", "chinese-v001.srt",
    )}
    for name, content in previous_files.items():
        (output_dir / name).write_bytes(content)

    with pytest.raises(RuntimeError, match="Injected"):
        renderer.render_project(
            tmp_path / "project", tmp_path / "source.mp4",
            [{"start_ms": 0, "end_ms": 1000, "source_text": "Hello", "target_text": "你好"}],
            True, output_dir=output_dir, progress_callback=progress, should_cancel=cancel_check,
        )

    process = processes[0]
    assert process.waited
    assert process.poll() is not None
    assert process.stdout.closed and process.stderr.closed
    assert process.terminated == (failure in {"progress", "cancel_check"})
    assert process.killed == slow_exit
    assert {path.name: path.read_bytes() for path in output_dir.iterdir()} == previous_files
