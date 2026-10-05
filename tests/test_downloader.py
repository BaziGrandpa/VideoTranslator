from pathlib import Path

import yt_dlp

from app.services.downloader import _english_caption, download_youtube


def test_english_caption_prefers_original_track() -> None:
    info = {
        "language": "en-US",
        "automatic_captions": {"en": [{}] * 147, "en-orig": [{}] * 7},
    }
    assert _english_caption(info) == ("en-orig", "youtube_auto")
    assert _english_caption({**info, "subtitles": {"en": [{}]}}) == (
        "en", "youtube_manual"
    )


def test_download_uses_only_selected_original_caption(tmp_path: Path, monkeypatch) -> None:
    calls = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.params = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def extract_info(self, _url, *, download):
            calls.append((download, self.params.get("subtitleslangs")))
            info = {
                "id": "example",
                "title": "Example",
                "language": "en-US",
                "duration": 10,
                "automatic_captions": {"en": [{}] * 147, "en-orig": [{}] * 7},
            }
            if download:
                (tmp_path / "source" / "video.mp4").write_bytes(b"video")
                (tmp_path / "source" / "video.en-orig.vtt").write_text("WEBVTT\n")
            return info

        def sanitize_info(self, info):
            return info

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)
    result = download_youtube("https://www.youtube.com/watch?v=example", tmp_path, 1080)
    assert calls == [(False, None), (True, ["en-orig"])]
    assert result["subtitle_path"].name == "video.en-orig.vtt"
    assert result["subtitle_source"] == "youtube_auto"


def test_caption_error_falls_back_to_video(tmp_path: Path, monkeypatch) -> None:
    calls = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.params = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def extract_info(self, _url, *, download):
            calls.append((download, self.params["writeautomaticsub"]))
            if download and self.params["writeautomaticsub"]:
                raise yt_dlp.utils.DownloadError(
                    "Unable to download video subtitles for 'en-orig': HTTP Error 429"
                )
            if download:
                (tmp_path / "source" / "video.mp4").write_bytes(b"video")
            return {
                "id": "example",
                "title": "Example",
                "language": "en-US",
                "duration": 10,
                "automatic_captions": {"en-orig": [{}]},
            }

        def sanitize_info(self, info):
            return info

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)
    result = download_youtube("https://www.youtube.com/watch?v=example", tmp_path, 1080)
    assert calls == [(False, False), (True, True), (True, False)]
    assert result["video_path"].name == "video.mp4"
    assert result["subtitle_path"] is None
    assert result["subtitle_source"] is None
