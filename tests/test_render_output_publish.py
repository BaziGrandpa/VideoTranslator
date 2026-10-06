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
