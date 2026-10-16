from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageChops

import app.main as main_module
from app.cards import api as cards_api
from app.cards.model import CardPayload
from app.cards.repository import CardRepository
from app.cards.visual import _font, _wrap, render_card_image
from app.config import command_available, settings
from app.db import SCHEMA, connect, init_db
from app.repository import Repository
from app.services.exact_preview import render_exact_preview
from app.services.renderer import render_project


def sample_card(start_ms: int = 500, end_ms: int = 2500) -> dict:
    return {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "chinese_term": "副交感神经",
        "english_term": "parasympathetic nervous system",
        "explanation": "指的是帮助身体休息和消化的神经系统。",
    }


def make_video(path: Path) -> None:
    result = subprocess.run(
        [settings.ffmpeg_bin, "-y", "-f", "lavfi", "-i",
         "color=c=navy:size=640x360:rate=30:duration=3", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(path)],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("stage", ["render_queued", "rendering", "render_cancelling"])
def test_card_mutations_locked_until_render_finishes(tmp_path: Path, monkeypatch, stage: str) -> None:
    database = tmp_path / "cards.db"
    init_db(database)
    repository = Repository(database)
    project = repository.create_project("https://youtu.be/example")
    project_id = project["id"]
    video = tmp_path / "source.mp4"
    video.write_bytes(b"placeholder")
    repository.update_project(
        project_id, video_path=str(video), duration_ms=10_000, status="running", stage=stage,
    )
    cards = CardRepository(database)
    card = cards.create(project_id, CardPayload.model_validate(sample_card()))
    before = cards.list(project_id)
    monkeypatch.setattr(main_module, "repository", repository)
    monkeypatch.setattr(cards_api, "probe_video", lambda _path: (640, 360))
    monkeypatch.setattr(cards_api, "render_card_png", lambda *_args: (b"image", {}))
    client = TestClient(main_module.app)
    route = f"/api/projects/{project_id}/cards"
    updated = {**sample_card(), "explanation": "Updated explanation"}

    assert client.post(route, json=sample_card(4000, 6500)).status_code == 409
    assert client.put(f"{route}/{card['id']}", json=updated).status_code == 409
    assert client.delete(f"{route}/{card['id']}").status_code == 409
    assert client.get(route).status_code == 200
    assert cards.list(project_id) == before
    with connect(database) as connection:
        assert connection.execute(
            "SELECT revision FROM commentary_card_render_state WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0] == 1

    repository.update_project(project_id, status="waiting_review", stage="waiting_review")
    assert client.post(route, json=sample_card(4000, 6500)).status_code == 201
    assert client.put(f"{route}/{card['id']}", json=updated).status_code == 200
    assert cards.get(project_id, card["id"])["explanation"] == "Updated explanation"
    assert client.delete(f"{route}/{card['id']}").status_code == 200


def test_existing_database_schema_gains_card_tables_without_changing_project(tmp_path: Path) -> None:
    database = tmp_path / "old.db"
    with connect(database) as connection:
        connection.executescript(SCHEMA)
    project = Repository(database).create_project("https://youtu.be/example")
    init_db(database)
    assert Repository(database).get_project(project["id"])["id"] == project["id"]
    assert CardRepository(database).list(project["id"]) == []


def test_latest_saved_card_style_becomes_default_for_existing_projects(tmp_path: Path) -> None:
    database = tmp_path / "cards.db"
    init_db(database)
    projects = Repository(database)
    first = projects.create_project("https://youtu.be/first")
    second = projects.create_project("https://youtu.be/second")
    cards = CardRepository(database)
    assert cards.default_style()["width_pct"] == 32
    assert cards.default_style()["side"] == "right"

    first_payload = CardPayload.model_validate({
        **sample_card(), "style": {"side": "left", "width_pct": 41,
                                   "title_color": "#FFCC00"},
    })
    saved = cards.create(first["id"], first_payload)
    assert cards.default_style() == saved["style"]
    assert CardRepository(database).default_style()["width_pct"] == 41
    assert CardRepository(database).default_style()["side"] == "left"

    latest_payload = CardPayload.model_validate({
        **sample_card(), "style": {"width_pct": 45, "fade_in_ms": 300},
    })
    cards.create(second["id"], latest_payload)
    assert cards.default_style()["width_pct"] == 45
    cards.update(first["id"], saved["id"], first_payload)
    assert cards.default_style() == first_payload.style.model_dump()
    cards.delete(first["id"], saved["id"])
    assert cards.default_style() == first_payload.style.model_dump()

    with connect(database) as connection:
        connection.execute("DELETE FROM app_settings WHERE key = 'default_commentary_card_style'")
    assert cards.default_style()["width_pct"] == 45


def test_card_text_wraps_and_rejects_unsafe_height() -> None:
    from PIL import ImageDraw

    drawing = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    assert _wrap(drawing, "parasympathetic nervous system", _font("Arial", 20, False), 400)
    long_card = CardPayload.model_validate({**sample_card(), "explanation": "解释内容" * 200})
    with pytest.raises(ValueError, match="safe area"):
        render_card_image(long_card, 640, 360)


def test_card_side_defaults_right_and_mirrors_in_video_coordinates() -> None:
    right = CardPayload.model_validate(sample_card())
    left = CardPayload.model_validate({**sample_card(), "style": {"side": "left"}})
    center = CardPayload.model_validate({**sample_card(), "style": {"side": "center"}})
    _, right_geometry = render_card_image(right, 640, 360)
    _, left_geometry = render_card_image(left, 640, 360)
    _, center_geometry = render_card_image(center, 640, 360)
    assert right.style.side == "right"
    assert left_geometry["x"] == 26
    assert right_geometry["x"] == 640 - 26 - right_geometry["width"]
    assert left_geometry["width"] == right_geometry["width"]
    assert center_geometry["x"] == (640 - center_geometry["width"]) // 2
    assert center_geometry["y"] == right_geometry["y"]


@pytest.mark.skipif(not command_available(settings.ffmpeg_bin), reason="FFmpeg unavailable")
def test_cards_are_independent_and_work_on_existing_project(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "existing.db"
    init_db(database)
    repository = Repository(database)
    project = repository.create_project("https://youtu.be/example")
    video = tmp_path / "source.mp4"
    make_video(video)
    repository.update_project(
        project["id"], video_path=str(video), duration_ms=3000,
        status="waiting_review", stage="waiting_review", rendered_revision=0,
    )
    monkeypatch.setattr(main_module, "repository", repository)
    client = TestClient(main_module.app)
    route = f"/api/projects/{project['id']}/cards"
    first = client.post(route, json=sample_card())
    assert first.status_code == 201, first.text
    card_id = first.json()["id"]
    assert first.json()["style"]["fade_in_ms"] == 800
    default_style = client.get(f"{route}/default-style")
    assert default_style.headers["cache-control"] == "no-store"
    assert default_style.json() == first.json()["style"]
    assert client.post(route, json=sample_card(2000, 2900)).status_code == 422
    touching = client.post(route, json=sample_card(2500, 3000))
    assert touching.status_code == 422  # default fades exceed a half-second card
    touching_payload = sample_card(2500, 3000)
    touching_payload["style"] = {"fade_in_ms": 200, "fade_out_ms": 200}
    assert client.post(route, json=touching_payload).status_code == 201
    details = client.get(f"/api/projects/{project['id']}").json()
    assert details["subtitle_revision"] == 0
    assert details["card_revision"] == 2
    assert details["render_is_stale"] is True
    assert client.get(route).json()[0]["id"] == card_id
    updated_payload = sample_card()
    updated_payload["style"] = {"side": "left", "width_pct": 40,
                                "title_color": "#FFFF00"}
    updated = client.put(f"{route}/{card_id}", json=updated_payload)
    assert updated.status_code == 200, updated.text
    assert updated.json()["style"]["width_pct"] == 40
    assert updated.json()["style"]["side"] == "left"
    assert client.get(f"{route}/default-style").json()["width_pct"] == 40
    visual = client.get(f"{route}/{card_id}/visual")
    assert visual.status_code == 200
    assert visual.content.startswith(b"\x89PNG")
    assert int(visual.headers["x-card-width"]) == 256
    assert int(visual.headers["x-card-x"]) == 26
    assert client.get(f"/api/projects/{project['id']}").json()["subtitle_revision"] == 0
    repository.clear_subtitle_data(project["id"])
    assert len(CardRepository(database).list(project["id"])) == 2
    assert client.delete(f"{route}/{card_id}").status_code == 200
    assert len(CardRepository(database).list(project["id"])) == 1


@pytest.mark.skipif(not command_available(settings.ffmpeg_bin), reason="FFmpeg unavailable")
@pytest.mark.parametrize("side", ["left", "center", "right"])
def test_card_exact_preview_and_final_render_share_visual(tmp_path: Path, side: str) -> None:
    video = tmp_path / "source.mp4"
    make_video(video)
    card = CardPayload.model_validate({**sample_card(), "style": {"side": side}}).model_dump()
    blank = Image.open(io.BytesIO(render_exact_preview(
        "card-preview", video, [], 100, False, None, cards=[card]
    ))).convert("RGB")
    full = Image.open(io.BytesIO(render_exact_preview(
        "card-preview", video, [], 1500, False, None, cards=[card]
    ))).convert("RGB")
    partial = Image.open(io.BytesIO(render_exact_preview(
        "card-preview", video, [], 900, False, None, cards=[card]
    ))).convert("RGB")
    _, geometry = render_card_image(card, 640, 360)
    sample = (geometry["x"] + geometry["width"] - 15, geometry["y"] + 15)
    assert sum(full.getpixel(sample)) < sum(blank.getpixel(sample)) - 20
    assert sum(full.getpixel(sample)) < sum(partial.getpixel(sample)) < sum(blank.getpixel(sample))

    output = render_project(
        tmp_path, video,
        [{"start_ms": 0, "end_ms": 3000, "source_text": "Hello", "target_text": "你好"}],
        bilingual=False, cards=[card],
    )
    probe = subprocess.run(
        [settings.ffprobe_bin, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,profile,pix_fmt", "-of", "json", str(output)],
        check=True, capture_output=True, text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream == {"codec_name": "h264", "profile": "High", "pix_fmt": "yuv420p"}
    frame = tmp_path / "rendered.png"
    result = subprocess.run(
        [settings.ffmpeg_bin, "-y", "-loglevel", "error", "-ss", "1.5", "-i",
         str(output), "-frames:v", "1", str(frame)],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    rendered = Image.open(frame).convert("RGB")
    assert sum(rendered.getpixel(sample)) < sum(blank.getpixel(sample)) - 20
    faded_frame = tmp_path / "faded.png"
    result = subprocess.run(
        [settings.ffmpeg_bin, "-y", "-loglevel", "error", "-ss", "0.9", "-i",
         str(output), "-frames:v", "1", str(faded_frame)],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    faded = Image.open(faded_frame).convert("RGB")
    assert abs(sum(faded.getpixel(sample)) - sum(partial.getpixel(sample))) < 30


@pytest.mark.skipif(not command_available(settings.ffmpeg_bin), reason="FFmpeg unavailable")
def test_exact_preview_uses_subtitles_active_at_requested_time(tmp_path: Path) -> None:
    video = tmp_path / "source.mp4"
    make_video(video)
    subtitles = [
        {"start_ms": 1000, "end_ms": 2000, "source_text": "Now visible", "target_text": "现在显示"}
    ]
    before = Image.open(io.BytesIO(render_exact_preview(
        "sub-preview", video, subtitles, 500, False, None
    ))).convert("RGB")
    during = Image.open(io.BytesIO(render_exact_preview(
        "sub-preview", video, subtitles, 1500, False, None
    ))).convert("RGB")
    assert ImageChops.difference(before, during).getbbox() is not None
